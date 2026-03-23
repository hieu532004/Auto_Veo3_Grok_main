import os

target_file = "API_text_to_video.py"

target = """\t\theaders = {
\t\t\t"Content-Type": "application/json",
\t\t\t"Authorization": f"Bearer {access_token}",
\t\t}"""

replacement = """\t\theaders = {
\t\t\t"Content-Type": "application/json",
\t\t\t"Authorization": f"Bearer {access_token}",
\t\t\t"Origin": "https://labs.google",
\t\t\t"Referer": "https://labs.google/",
\t\t\t"X-Goog-AuthUser": "0",
\t\t}"""

if os.path.exists(target_file):
    with open(target_file, "r", encoding="utf-8") as f:
        data = f.read()
    if target in data:
        data = data.replace(target, replacement)
        with open(target_file, "w", encoding="utf-8") as f:
            f.write(data)
        print(f"Patched {target_file}")
    else:
        print("Target string not found in API_text_to_video.py")
