# Synthetic "photos" for the mockups: sky/ground gradients with simple objects, so thumbnails read as images.
import random
from PIL import Image, ImageDraw, ImageFilter
random.seed(7)
SCENES = [((96,140,190),(70,78,84)), ((230,170,110),(90,80,70)), ((60,80,120),(40,46,52)), ((150,190,220),(80,120,70)),
          ((200,200,205),(120,118,112)), ((40,40,60),(30,30,34))]
for i in range(30):
    sky, gnd = random.choice(SCENES)
    w, h = 480, 360
    im = Image.new('RGB', (w, h))
    d = ImageDraw.Draw(im)
    hz = random.randint(140, 220)
    for y in range(h):
        c = sky if y < hz else gnd
        k = (y / hz) if y < hz else (y - hz) / (h - hz)
        d.line([(0, y), (w, y)], fill=tuple(int(v * (0.75 + 0.35 * k)) for v in c))
    for _ in range(random.randint(2, 5)):
        x = random.randint(0, w - 120); y = random.randint(hz - 40, h - 60)
        bw, bh = random.randint(50, 140), random.randint(30, 80)
        col = random.choice([(180,40,40),(30,60,140),(220,220,220),(40,40,40),(200,160,30),(60,120,70)])
        d.rounded_rectangle([x, y, x + bw, y + bh], 8, fill=col)
        d.ellipse([x + 8, y + bh - 14, x + 30, y + bh + 6], fill=(20,20,20))
        d.ellipse([x + bw - 30, y + bh - 14, x + bw - 8, y + bh + 6], fill=(20,20,20))
    if random.random() < .5:
        for _ in range(3):
            x = random.randint(0, w); d.rectangle([x, hz - random.randint(60,140), x + random.randint(20,50), hz], fill=tuple(v//2 for v in sky))
    im = im.filter(ImageFilter.GaussianBlur(0.8))
    im.save(f'samples/img_{i:02d}.jpg', quality=82)
print('ok')
