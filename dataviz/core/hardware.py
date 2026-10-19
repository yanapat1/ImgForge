"""
Hardware profile: which device to run on, how many workers and how big a batch fits.

    from dataviz.core import hardware
    hardware.device()                  # 'cuda' or 'cpu' (the GPU must exist AND pass a tiny self-test)
    hardware.dtype()                   # torch dtype for GPU inference (bf16/fp16), float32 on CPU
    hardware.workers('io')             # thread/process count for image decoding
    hardware.pick_batch(per_sample_bytes, cap=128)
    hardware.with_fallback(fn, batch)  # fn(batch) -> result; halves the batch on CUDA OOM, then falls back to CPU

Override with `DATAVIZ_DEVICE=cpu|cuda`, or `configure({'device': 'cpu', ...})` from config.yaml `hardware:`.
The profile is computed once per process (cheap: one tiny matmul) and never raises: any failure means CPU.
"""

import os
import threading
import time

_LOCK = threading.Lock()
_PROFILE = None
_SETTINGS = {'device': 'auto', 'precision': 'auto'}
HEADROOM = 0.2          # keep this share of free VRAM unused (the GPU / MIG slice can be shared)


def configure(settings=None):
    """Apply config.yaml `hardware:` ({'device': auto|cpu|cuda, 'precision': auto|fp32|fp16|bf16})."""
    global _PROFILE
    with _LOCK:
        _SETTINGS.update({k: v for k, v in (settings or {}).items() if v is not None})
        _PROFILE = None


def _cpu_count():
    """CPUs this process may really use: affinity, further capped by a cgroup quota."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    for path in ('/sys/fs/cgroup/cpu.max',):                       # cgroup v2: "max 100000" or "200000 100000"
        try:
            quota, period = open(path).read().split()[:2]
            if quota != 'max':
                n = min(n, max(1, int(int(quota) / int(period))))
        except (OSError, ValueError):
            pass
    try:                                                           # cgroup v1
        q = int(open('/sys/fs/cgroup/cpu/cpu.cfs_quota_us').read())
        p = int(open('/sys/fs/cgroup/cpu/cpu.cfs_period_us').read())
        if q > 0 and p > 0:
            n = min(n, max(1, q // p))
    except (OSError, ValueError):
        pass
    return n


def _ram_bytes():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    return 0


def _gpu_probe():
    """-> dict(name, free, total, bf16, speedup) or None when no usable GPU. Never raises."""
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        free, total = torch.cuda.mem_get_info()
        a = torch.randn(2048, 2048, device='cuda', dtype=torch.float16)
        for _ in range(3):                                          # warm-up: CUDA context + cuBLAS init are not "speed"
            (a @ a).sum().item()
        torch.cuda.synchronize()
        gpu = cpu = float('inf')
        b = torch.randn(2048, 2048, dtype=torch.float32)
        for _ in range(3):                                          # best of 3: robust against a loaded, shared host
            t = time.perf_counter()
            (a @ a).sum().item()                                    # .item() syncs and proves results come back
            gpu = min(gpu, time.perf_counter() - t)
            t = time.perf_counter()
            (b @ b).sum().item()
            cpu = min(cpu, time.perf_counter() - t)
        del a, b
        return {'name': torch.cuda.get_device_name(0), 'free': int(free), 'total': int(total),
                'bf16': bool(torch.cuda.is_bf16_supported()), 'speedup': cpu / max(gpu, 1e-9)}
    except Exception:                                               # broken driver / MIG misconfig / OOM at init
        return None


def profile(refresh=False):
    global _PROFILE
    with _LOCK:
        if _PROFILE is not None and not refresh:
            return _PROFILE
        want = os.environ.get('DATAVIZ_DEVICE') or _SETTINGS.get('device') or 'auto'
        gpu = None if want == 'cpu' else _gpu_probe()
        use_gpu = gpu is not None and (want == 'cuda' or gpu['speedup'] > 1.0)
        prec = _SETTINGS.get('precision') or 'auto'
        if prec == 'auto':
            prec = ('bf16' if gpu and gpu['bf16'] else 'fp16') if use_gpu else 'fp32'
        cpus = _cpu_count()
        _PROFILE = {'device': 'cuda' if use_gpu else 'cpu', 'precision': prec if use_gpu else 'fp32',
                    'gpu': gpu, 'cpus': cpus, 'ram': _ram_bytes()}
        return _PROFILE


def device():
    return profile()['device']


def dtype():
    import torch
    return {'bf16': torch.bfloat16, 'fp16': torch.float16}.get(profile()['precision'], torch.float32)


def demote_to_cpu():
    """After a CUDA failure at run time: stay on CPU for the rest of the process."""
    global _PROFILE
    with _LOCK:
        base = _PROFILE or {'cpus': _cpu_count(), 'ram': _ram_bytes(), 'gpu': None}
        _PROFILE = {**base, 'device': 'cpu', 'precision': 'fp32'}


def workers(kind='io'):
    """Parallelism for `io` (image decode threads, which release the GIL), `proc` (decode processes), `omp` (BLAS/numba threads)."""
    n = profile()['cpus']
    if kind == 'proc':
        return max(1, min(n - 1, 32))
    if kind == 'omp':
        return max(1, min(n // 2, 16))
    return max(4, min(n, 32))


def pick_batch(per_sample_bytes, cap=128, floor=1):
    """Largest batch (<= cap) whose activations fit in free VRAM minus headroom; `cap` on CPU."""
    p = profile()
    if p['device'] != 'cuda' or not p['gpu']:
        return cap
    try:
        import torch
        free = torch.cuda.mem_get_info()[0]
    except Exception:
        free = p['gpu']['free']
    return int(max(floor, min(cap, free * (1 - HEADROOM) // max(1, per_sample_bytes))))


def is_oom(exc):
    try:
        import torch
        if isinstance(exc, torch.cuda.OutOfMemoryError):
            return True
    except Exception:
        pass
    return 'out of memory' in str(exc).lower()


def with_fallback(fn, batch, on_cpu=None):
    """fn(batch) on the current device. CUDA OOM → retry on two halves (recursively); another CUDA error → demote to
    CPU and call `on_cpu(batch)` (default `fn`, which should then re-read `device()`). Returns a list of results."""
    try:
        return [fn(batch)]
    except Exception as e:
        if device() != 'cuda':
            raise
        if is_oom(e) and len(batch) > 1:
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
            mid = len(batch) // 2
            return with_fallback(fn, batch[:mid], on_cpu) + with_fallback(fn, batch[mid:], on_cpu)
        if not (is_oom(e) or 'cuda' in str(e).lower()):
            raise
        demote_to_cpu()
        return [(on_cpu or fn)(batch)]


def describe():
    p = profile()
    g = p['gpu']
    gpu = f"{g['name']} {g['free'] / 2**30:.0f}/{g['total'] / 2**30:.0f} GiB free" if g else 'none'
    return f"device={p['device']} ({p['precision']}) gpu={gpu} cpus={p['cpus']} ram={p['ram'] / 2**30:.0f} GiB"
