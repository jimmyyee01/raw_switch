import sys
sys.path.insert(0, 'C:/Users/MAMA TECH SOLUTIONS')

import sim_aggregator.switching as s
print('switching.py imports OK')
print('Available functions:', [f for f in dir(s) if not f.startswith('_')])
