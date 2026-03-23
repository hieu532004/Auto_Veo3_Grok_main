import codecs

with codecs.open('A_workflow_sync_chactacter.py', 'r', 'utf-8') as f:
    lines = f.readlines()

new_lines = []
in_collector_init = False
collector_init_code = []

for i, line in enumerate(lines):
    if line.startswith('        profile_name = self.project_data.get(\"veo_profile\")') and 'profile_name' in line and i > 260 and i < 280:
        in_collector_init = True
        
    if in_collector_init:
        collector_init_code.append(line)
        if line.startswith('            return'):
            # Check if previous line had 'Traceback'
            # actually we can just look for the end of the except block
            pass
        if line.strip() == 'return' and 'Traceback' in lines[i-3]:
            in_collector_init = False
        continue

    new_lines.append(line)

# Let me just use replace file content for precise changes.
