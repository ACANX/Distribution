import json
import os
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("Data/Meta/WebMMCP/APM/API")
ARCHIVE_DIR = Path("Archive/Meta/WebMMCP/APM/API")

def merge_json_files():
    json_files = sorted(DATA_DIR.glob("*.json"))
    
    merged_data = []
    for file_path in json_files:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                merged_data.extend(data)
            else:
                merged_data.append(data)
    
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    output_file = ARCHIVE_DIR / f"{timestamp}.json"
    
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(merged_data, f, ensure_ascii=False)
    
    print(f"Merged {len(json_files)} files into {output_file}")
    return output_file

if __name__ == "__main__":
    merge_json_files()
