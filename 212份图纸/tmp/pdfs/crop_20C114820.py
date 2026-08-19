from pathlib import Path
from PIL import Image, ImageDraw

SRC = Path("tmp/pdfs/20C114820_full.png")
OUT = Path("outputs/20C114820_assets/images")

OUT.mkdir(parents=True, exist_ok=True)
for old in OUT.glob("*.png"):
    old.unlink()

img = Image.open(SRC)

crops = {
    "20C114820_full_page.png": (0, 0, 4959, 3505),
    "01_revision_table.png": (2780, 165, 4805, 465),
    "02_attached_table_1.png": (380, 485, 1675, 935),
    "03_fig2_axial_test.png": (1745, 365, 2265, 1065),
    "04_attached_table_2_test_list.png": (375, 1060, 2545, 3335),
    "05_main_view.png": (2590, 675, 3560, 1235),
    "06_section_A_A.png": (3615, 690, 4160, 1215),
    "07_marking_local_view.png": (4215, 690, 4685, 1135),
    "08a_technical_requirements_1_4.png": (2765, 1795, 4325, 2115),
    "08b_technical_requirements_5_8.png": (2765, 2130, 3910, 2345),
    "09_fig1_product_orientation.png": (3960, 1950, 4710, 2465),
    "10_parts_list_and_title_block.png": (2770, 2390, 4815, 3355),
}

for name, box in crops.items():
    crop = img.crop(box)
    if name == "09_fig1_product_orientation.png":
        draw = ImageDraw.Draw(crop)
        draw.rectangle((0, 0, 360, 132), fill="white")
        draw.rectangle((345, 80, 400, 135), fill="white")
        draw.rectangle((0, 132, 120, 190), fill="white")
    crop.save(OUT / name)
    print(name, box)
