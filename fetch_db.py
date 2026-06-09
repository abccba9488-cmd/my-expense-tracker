import urllib.request, zipfile, os, shutil

URL = "https://github.com/abccba9488-cmd/my-expense-tracker/releases/download/db-v1/stocks.db.zip"
ZIP = "/tmp/stocks.db.zip"
DST = "/app/data/stocks.db"

print("Downloading...")
urllib.request.urlretrieve(URL, ZIP)
print("Extracting...")
with zipfile.ZipFile(ZIP, 'r') as z:
    z.extractall("/tmp")
src = "/tmp/stocks_backup.db"
os.makedirs(os.path.dirname(DST), exist_ok=True)
shutil.move(src, DST)
os.remove(ZIP)
print(f"Done: {DST} ({os.path.getsize(DST)//1024//1024} MB)")
