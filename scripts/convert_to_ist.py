import json
from datetime import datetime, timezone, timedelta
import sys

# IST timezone = UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))

def unix_to_ist_str(ts):
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    dt_ist = dt_utc.astimezone(IST)
    return dt_ist.strftime('%Y-%m-%d %H:%M:%S')

def convert_file_in_place(filename):
    # Read all lines
    with open(filename, 'r') as f:
        lines = f.readlines()

    converted_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if 'ts' in obj:
                ist_str = unix_to_ist_str(obj['ts'])
                # Option 1: Replace the 'ts' field with IST string
                obj['ts'] = ist_str
                # Option 2 (optional): Keep original ts as 'ts_unix' and add 'ts_ist'
                # obj['ts_ist'] = ist_str
                converted_lines.append(json.dumps(obj))
            else:
                # No ts field; keep as is
                converted_lines.append(line)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"Skipping invalid line: {line[:50]}... Error: {e}", file=sys.stderr)
            converted_lines.append(line)  # keep original if conversion fails

    # Write back to the same file
    with open(filename, 'w') as f:
        f.write('\n'.join(converted_lines) + ('\n' if converted_lines else ''))

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Usage: python convert_to_ist.py <filename>")
        sys.exit(1)
    convert_file_in_place(sys.argv[1])
