from PIL import Image
import numpy as np

img = Image.open('tools/_f.jpg')
arr = np.asarray(img.convert('RGB'), dtype=np.int32)
# hero card 1 (4h) at (249,276) 36x38, card 2 (2c) at (287,276) 36x32
for name, (x, y, bw, bh) in [('card1(4h)', (249,276,36,38)), ('card2(2c)', (287,276,36,32))]:
    sub = arr[y:y+bh, x:x+bw]
    R, G, B = sub[...,0], sub[...,1], sub[...,2]
    print(name, 'size', sub.shape)
    print('  median RGB:', int(np.median(R)), int(np.median(G)), int(np.median(B)))
    # white fraction
    white = ((R>170)&(G>170)&(B>170)).mean()
    print('  white frac:', round(white,2))
    # top-left corner (rank area) colors
    tl = sub[:bh//2, :bw//2]
    print('  top-left median:', int(np.median(tl[...,0])), int(np.median(tl[...,1])), int(np.median(tl[...,2])))
    # dominant non-white color
    nonwhite = (R<170)|(G<170)|(B<170)
    if nonwhite.any():
        print('  non-white median:', int(np.median(R[nonwhite])), int(np.median(G[nonwhite])), int(np.median(B[nonwhite])))
