import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from PIL import Image
import mirror_server as ms
import cv2

img = Image.open('tools/_f.jpg')
w, h = img.size
arr = np.asarray(img.convert('RGB'), dtype=np.int32)
R, G, B = arr[...,0], arr[...,1], arr[...,2]

# hero cards: bottom-center region. Find WHITE card bodies (cards are white-ish?)
white = (R > 170) & (G > 170) & (B > 170)
mask = (white.astype(np.uint8)) * 255
n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
print('white blobs in full frame (area > 30):')
for i in range(1, n):
    x, y, bw, bh, area = stats[i]
    if area > 30 and y > 0.5*h:
        print(f'  @({x},{y}) {bw}x{bh} frac=({x/w:.2f},{y/h:.2f},{bw/w:.2f},{bh/h:.2f})')

# Also check colored (red/green) text in bottom-center
for name, cond in [('red', (R>140)&(G<100)&(B<100)), ('green', (G>120)&(R<100)&(B<100)), ('blue', (B>120)&(R<100))]:
    m = (cond.astype(np.uint8))*255
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    print(f'{name} blobs (area>20, y>0.5h):')
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area > 20 and y > 0.5*h:
            print(f'    @({x},{y}) {bw}x{bh} frac=({x/w:.2f},{y/h:.2f})')
