import os
import glob

files = glob.glob("API_*.py")

for fpath in files:
    with open(fpath, "r", encoding="utf-8") as f:
        data = f.read()
    
    # 4 spaces
    target_1 = '''        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        }'''
    rep_1 = '''        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Origin": "https://labs.google",
            "Referer": "https://labs.google/",
            "X-Goog-AuthUser": "0",
        }'''

    # 3 spaces? maybe
    target_2 = '''\t\theaders = {
\t\t\t"Content-Type": "application/json",
\t\t\t"Authorization": f"Bearer {access_token}",
\t\t}'''
    rep_2 = '''\t\theaders = {
\t\t\t"Content-Type": "application/json",
\t\t\t"Authorization": f"Bearer {access_token}",
\t\t\t"Origin": "https://labs.google",
\t\t\t"Referer": "https://labs.google/",
\t\t\t"X-Goog-AuthUser": "0",
\t\t}'''

    data = data.replace(target_1, rep_1).replace(target_2, rep_2)
    
    # And handle token
    target_3 = '''        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }'''
    rep_3 = '''        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": "https://labs.google",
            "Referer": "https://labs.google/",
            "X-Goog-AuthUser": "0",
        }'''
    data = data.replace(target_3, rep_3)
    
    target_4 = '''\t\theaders = {
\t\t\t"Content-Type": "application/json",
\t\t\t"Authorization": f"Bearer {token}",
\t\t}'''
    rep_4 = '''\t\theaders = {
\t\t\t"Content-Type": "application/json",
\t\t\t"Authorization": f"Bearer {token}",
\t\t\t"Origin": "https://labs.google",
\t\t\t"Referer": "https://labs.google/",
\t\t\t"X-Goog-AuthUser": "0",
\t\t}'''
    data = data.replace(target_4, rep_4)

    with open(fpath, "w", encoding="utf-8") as f:
        f.write(data)
    print(f"Patched strictly {fpath}")

