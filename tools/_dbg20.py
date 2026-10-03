import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
import mirror_server as ms

img = Image.open('tools/_f.jpg')
w, h = img.size
print('frame:', w, 'x', h)
blobs = ms._find_card_blobs(img)
print('all card blobs (y0f=0.40):', len(blobs))
for x, y, bw, bh, area in blobs:
    for sx, sy, sw, sh in ms._split_wide(x, y, bw, bh):
        card = img.crop((sx, sy, sx+sw, sy+sh))
        rgb = ms._body_color(card)
        rank = ms._read_rank(card)
        suit = ms._suit_from_color(rgb)
        print(f'  @({sx},{sy}) {sw}x{sh} frac_y={sy/h:.2f} rgb={rgb} -> {rank}{suit}')
