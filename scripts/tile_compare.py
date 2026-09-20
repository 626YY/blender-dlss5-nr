"""tile_compare.py out.png x0 y0 x1 y1 label=path [label=path ...]  (paths at 2x scale get auto-downscaled)"""
import sys
from PIL import Image, ImageDraw
out = sys.argv[1]; x0,y0,x1,y1 = map(int, sys.argv[2:6]); items = [a.split("=",1) for a in sys.argv[6:]]
tiles = []
for label, path in items:
    im = Image.open(path).convert("RGB")
    if im.width >= 3000: im = im.resize((im.width//2, im.height//2), Image.LANCZOS)
    c = im.crop((x0,y0,x1,y1)); d = ImageDraw.Draw(c); d.rectangle((0,0,len(label)*7+8,16), fill=(0,0,0)); d.text((4,2), label, fill=(255,255,0)); tiles.append(c)
w,h = tiles[0].size; cols = 2 if len(tiles) > 2 else len(tiles); rows = (len(tiles)+cols-1)//cols
sheet = Image.new("RGB", (w*cols, h*rows))
for i,t in enumerate(tiles): sheet.paste(t, ((i%cols)*w, (i//cols)*h))
sheet.save(out); print("saved", out, sheet.size)
