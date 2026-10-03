from PIL import Image
img = Image.open('tools/_f.jpg')
w, h = img.size
# bottom CENTER (hero)
crop = img.crop((int(0.38*w), int(0.52*h), int(0.62*w), int(0.85*h)))
crop = crop.resize((crop.width*4, crop.height*4), Image.NEAREST)
crop.save('tools/_hero_zoom3.jpg')
print('saved, frame', w, h)
