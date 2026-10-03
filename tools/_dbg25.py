import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
import mirror_server as ms

img = Image.open('tools/_f.jpg')
print('detect result:', ms.detect(img))

# also check blobs + card reading directly
blobs = ms._find_card_blobs(img)
print('blobs:', len(blobs))
for x, y, bw, bh, area in blobs:
    for sx, sy, sw, sh in ms._split_wide(x, y, bw, bh):
        card = img.crop((sx, sy, sx+sw, sy+sh))
        rank = ms._read_rank(card)
        suit = ms._suit_from_card(card)
        print(f'  @({sx},{sy}) {sw}x{sh} frac_y={sy/img.height:.2f} -> rank={rank} suit={suit}')
