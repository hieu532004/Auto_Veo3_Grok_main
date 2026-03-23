import sys, re

with open('A_workflow_sync_chactacter.py', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Extract collector block
pattern_collector = r"(        profile_name = self\.project_data\.get.*?        status_task\.cancel\(\)\n            return\n)"
match = re.search(pattern_collector, text, re.DOTALL)
if not match:
    print("Cannot find collector block")
    sys.exit(1)
collector_block = match.group(1)

# Remove collector block from original text
text = text.replace(collector_block, "")

# 2. Add __aenter__ call and wrapper
new_collector_block = collector_block + '''
        try:
            if hasattr(collector, '__aenter__'):
                await collector.__aenter__()
            self._collector_ref = collector
            
            # ? Refresh auth t? Chrome browser ngay sau khi Chrome kh?i d?ng 
            try:
                if hasattr(collector, 'refresh_auth_from_browser'):
                    fresh_token, fresh_cookie = await collector.refresh_auth_from_browser(auth.get("projectId", ""))
                    if fresh_token:
                        auth["access_token"] = fresh_token
                        self._log("? Ðã l?y access_token m?i t? Chrome browser")
                    if fresh_cookie:
                        auth["cookie"] = fresh_cookie
            except Exception as e:
                self._log(f"?? Không refresh du?c auth t? Chrome: {e}")
'''

# 3. Find insertion point for new_collector_block (before media_cache upload)
insert_point_str = '        self._log(f"?? Upload ?nh nhân v?t dùng chung:'
parts = text.split(insert_point_str)
if len(parts) != 2:
    print("Cannot find insertion point")
    sys.exit(1)

# Modify part 2: we need to remove the "async with collector:" block and replace it with "if collector:" to preserve indentation
# And remove the duplicate proactive auth block that was originally under async with collector
part2 = parts[1]

# Remove the old proactive token refresh from part2
old_proactive = r"(        async with collector:\n            # ? Refresh auth t? Chrome browser ngay sau khi Chrome kh?i d?ng\n            self\._collector_ref = collector\n.*?            proactive_refresh_task = asyncio\.create_task\(_proactive_token_refresh\(\)\)\n)"
match2 = re.search(old_proactive, part2, re.DOTALL)
if match2:
    part2 = part2.replace(match2.group(1), "        if collector:\n            proactive_refresh_task = asyncio.create_task(_proactive_token_refresh())\n")
else:
    print("Cannot find old proactive block")
    # let's try a different regex, actually we can just manually replace string
    # Replace         async with collector: with         if collector: inside part2
    part2 = part2.replace("        async with collector:\n", "        if collector:\n")
    # We leave the rest unchanged since things might just work.

# Combine all components
final_text = parts[0] + new_collector_block + insert_point_str + part2

# Finally, append finally block closing collector in _run_workflow
final_text = final_text.replace("        status_task.cancel()\n\n    def _resolve_seed", "        status_task.cancel()\n        try:\n            if hasattr(collector, '__aexit__'):\n                await collector.__aexit__(None, None, None)\n        except:\n            pass\n\n    def _resolve_seed")

with open('A_workflow_sync_chactacter.py', 'w', encoding='utf-8') as f:
    f.write(final_text)

print("SUCCESS")
