import os
import glob
import re

files = glob.glob("API_*.py")

for fpath in files:
    with open(fpath, "r", encoding="utf-8") as f:
        data = f.read()

    # Match the entire header dict string if it lacks X-Goog-AuthUser
    # Wait, simple way: we find any header block containing 'Bearer {access_token}' or 'Bearer {token}' 
    # but not containing 'Origin'
    blocks_found = re.finditer(r'([ \t]+)headers = \{\n(.*?)\}', data, re.DOTALL)
    
    new_data = data
    for match in blocks_found:
        block = match.group(0)
        indent = match.group(1)
        inner = match.group(2)
        
        if "Bearer {" in block and "Origin" not in block:
            new_inner = inner
            
            # Find inner indent
            lines = inner.split('\n')
            inner_indent = ""
            for line in lines:
                if "Authorization" in line:
                    inner_indent = line[:len(line) - len(line.lstrip())]
                    break
            if not inner_indent:
                inner_indent = indent + "    "
                
            new_block = (f'{indent}headers = {{\n'
                         f'{inner}'
                         f'{inner_indent}"Origin": "https://labs.google",\n'
                         f'{inner_indent}"Referer": "https://labs.google/",\n'
                         f'{inner_indent}"X-Goog-AuthUser": "0",\n'
                         f'{indent}}}')
            
            new_data = new_data.replace(block, new_block)

    if new_data != data:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_data)
        print(f"Patched regex general {fpath}")

