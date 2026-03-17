import os

files = [
    "A_workflow_text_to_video.py",
    "A_workflow_image_to_video.py",
    "A_workflow_sync_chactacter.py",
    "A_workflow_generate_image.py",
    "A_workflow_image_to_image.py",
]

base = os.path.dirname(os.path.abspath(__file__))

for fn in files:
    fp = os.path.join(base, fn)
    if not os.path.exists(fp):
        print(f"SKIP: {fn} not found")
        continue
    with open(fp, "r", encoding="utf-8") as f:
        lines = f.readlines()
    changed = 0
    for i in range(len(lines)):
        if "TOKEN_OPTION" in lines[i] and "token_option" in lines[i] and "=" in lines[i]:
            indent = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            lines[i] = indent + 'token_option = "Option 1"\n'
            changed += 1
    if changed:
        with open(fp, "w", encoding="utf-8") as f:
            f.writelines(lines)
    print(f"{fn}: changed {changed} lines")
