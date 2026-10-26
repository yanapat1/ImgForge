"""Write a small "embeddings" session folder from scikit-learn's handwritten digits (no download).

    python examples/make_embeddings_demo.py [out_dir]       (default: examples/digits-embeddings)

Then in the page: New session › Embeddings folder › that folder. It holds 1797 rows with
    meta.csv      label (the digit), image (an upscaled PNG), ink (numeric), parity (categorical), text
    pixels.npy    the raw 64 pixel values
    pca16.npy     16 PCA components, L2-normalized (compared by cosine)
"""

import csv
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from sklearn.datasets import load_digits
from sklearn.decomposition import PCA


def main(out):
    out = Path(out)
    (out / 'images').mkdir(parents=True, exist_ok=True)
    d = load_digits()
    x = d.data.astype(np.float32)
    np.save(out / 'pixels.npy', x)
    pca = PCA(16, random_state=0).fit_transform(x)
    np.save(out / 'pca16.npy', (pca / np.linalg.norm(pca, axis=1, keepdims=True)).astype(np.float32))
    words = ['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine']
    with open(out / 'meta.csv', 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['id', 'label', 'image', 'text', 'ink', 'parity'])
        for i, (img, y) in enumerate(zip(d.images, d.target)):
            name = f'images/{i:04d}.png'
            Image.fromarray((255 - img * 16).clip(0, 255).astype(np.uint8)).resize((64, 64), Image.NEAREST).save(out / name)
            w.writerow([f'digit-{i:04d}', words[y], name, f'a handwritten {words[y]}', round(float(img.sum()), 1),
                        'even' if y % 2 == 0 else 'odd'])
    print(f'wrote {len(d.target)} rows to {out}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / 'digits-embeddings')
