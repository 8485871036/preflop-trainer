import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
import mirror_server as ms

img = Image.open('tools/_f.jpg')
# hero card 2 (2c) at ~(286,275) 37x40
card = img.crop((286, 275, 323, 315))
glyph = ms._rank_glyph(card)
print('glyph size:', glyph.size if glyph else None)
if glyph:
    key = ms._glyph_key(glyph)
    tm = ms._match_template(key)
    print('template match:', tm)
    # tesseract direct
    s = max(glyph.size) + 12
    canvas = Image.new('L', (s, s), 0)
    canvas.paste(glyph, ((s-glyph.width)//2, (s-glyph.height)//2))
    canvas = canvas.resize((s*8, s*8), Image.NEAREST)
    for psm in ('13','8','6','10','7'):
        t = ms._ocr_binary(canvas, 'AKQJT98765432', psm).strip()
        print(f'  tesseract psm{psm}: {t!r}')
    # save the glyph zoom for visual
    canvas.save('tools/_glyph2.jpg')

# also card 1 (4h)
card1 = img.crop((249, 275, 286, 315))
g1 = ms._rank_glyph(card1)
if g1:
    tm1 = ms._match_template(ms._glyph_key(g1))
    print('card1 (4h) template match:', tm1)
    s = max(g1.size) + 12
    c = Image.new('L', (s,s), 0)
    c.paste(g1, ((s-g1.width)//2, (s-g1.height)//2))
    c = c.resize((s*8, s*8), Image.NEAREST)
    c.save('tools/_glyph4.jpg')
