import os, zipfile, datetime

def create_project_backup():
    src_dir = "/storage/emulated/0/AutoMind_Ultimate_3.0_Audited"
    dest_dir = "/storage/emulated/0/AutoMind_Backups"
    os.makedirs(dest_dir, exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(dest_dir, f"AutoMind_Backup_{timestamp}.zip")
    
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(src_dir):
            if ".git" in root or "__pycache__" in root:
                continue
            for file in files:
                if file.endswith(".zip"):
                    continue
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, src_dir)
                zipf.write(full_path, rel_path)
                
    return f"Backup created successfully at: {zip_path}"

if __name__ == "__main__":
    print(create_project_backup())
