# -*- coding: utf-8 -*-
import sys

def patch_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    out_lines = []
    
    for i, line in enumerate(lines):
        if 'for retry_count in range(retry_with_error):' in line:
            indent = line[:line.find('for')]
            out_lines.append(indent + 'retry_count = 0\n')
            out_lines.append(indent + 'while retry_count < retry_with_error:\n')
        elif 'if retry_count > 0: retry_count -= 1' in line:
            # We remove these compensations, as we just won't increment retry_count anymore
            continue
        elif 'if retry_count < retry_with_error - 1:' in line and 'wait_resend_image' in lines[i+1]:
            # This is a generic error fallback, we must increment retry_count here!
            indent = line[:line.find('if')]
            out_lines.append(indent + 'retry_count += 1\n')
            out_lines.append(line)
        elif 'if retry_count < retry_with_error - 1:' in line:
            # Other fallback exceptions...
            indent = line[:line.find('if')]
            out_lines.append(indent + 'retry_count += 1\n')
            out_lines.append(line)
        else:
            out_lines.append(line)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.writelines(out_lines)

patch_file('A_workflow_generate_image.py')
