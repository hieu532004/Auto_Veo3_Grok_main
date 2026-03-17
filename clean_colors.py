import os, re
color_re = re.compile(r'(?i)\s*color\s*:\s*#([0-9a-f]{3,6})\b\s*;?')
changed_files = []
for root_dir in ['d:/Private_Projects/Source/MMO_TOOL/VEO_4.0_V2.2.6', 'd:/Private_Projects/Source/MMO_TOOL/VEO_4.0_V2.2.6/qt_ui']:
    for f in os.listdir(root_dir):
        if not f.endswith('.py'): continue
        if f == 'theme_manager.py': continue
        p = os.path.join(root_dir, f)
        if not os.path.isfile(p): continue
        with open(p, 'r', encoding='utf-8') as file:
            content = file.read()
        
        def repl(m):
            c = m.group(1).lower()
            if c in ['1f2d48', '334155', '0f172a', '1a1a2e', '31456a', '233c6a', '1e2d48', '333', '1e1e1e']:
                return ''
            return m.group(0)
            
        new_content = color_re.sub(repl, content)
        if new_content != content:
            with open(p, 'w', encoding='utf-8') as file:
                file.write(new_content)
            changed_files.append(f)

print('Changed:', changed_files)
