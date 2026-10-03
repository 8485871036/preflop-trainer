import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
import mirror_server as ms

img = Image.open('tools/_f.jpg')
w, h = img.size
# hero cards region (bottom-center)
crop = img.crop((int(0.60*w), int(0.55*h), int(0.90*w), int(0.80*h)))
crop = crop.resize((crop.width*4, crop.height*4), Image.NEAREST)
crop.save('tools/_hero_zoom2.jpg')
print('saved zoom, frame', w, h)

# Check the exact hero card location: find the two card blobs again but with lower y
for y0f in (0.50, 0.55, 0.60):
    blobs = ms._find_card_blobs(img, y0f=y0f, y1f=1.0)
    print(f'y0f={y0f}: {len(blobs)} blobs', [(b[0], b[1], b[2], b[3]) for b in blobs])
