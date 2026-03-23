import os
import glob
import re

files = glob.glob("API_*.py")

for fpath in files:
    with open(fpath, "r", encoding="utf-8") as f:
        data = f.read()
    
    # We want to find:
    #         headers = {
    #             "Content-Type": "application/json",
    #             "Authorization": f"Bearer {access_token}",
    #         }
    # Or variants with tabs or different indentation.
    
    # Let's search for the pattern
    pattern = r'([ \t]+)headers = \{\n([ \t]+)"Content-Type": "application/json",\n[ \t]+"Authorization": f"Bearer \{access_token\}",\n[ \t]+\}'
    
    def repl(match):
        indent_outer = match.group(1)
        indent_inner = match.group(2)
        return (f'{indent_outer}headers = {{\n'
                f'{indent_inner}"Content-Type": "application/json",\n'
                f'{indent_inner}"Authorization": f"Bearer {{access_token}}",\n'
                f'{indent_inner}"Origin": "https://labs.google",\n'
                f'{indent_inner}"Referer": "https://labs.google/",\n'
                f'{indent_inner}"X-Goog-AuthUser": "0",\n'
                f'{indent_outer}}}')

    new_data = re.sub(pattern, repl, data)
    if new_data != data:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_data)
        print(f"Patched regex {fpath}")

