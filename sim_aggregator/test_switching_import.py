import sys
sys.path.insert(0, 'C:/Users/MAMA TECH SOLUTIONS')

from sim_aggregator import switching

print("Import OK")
print("Functions:", [f for f in dir(switching) if not f.startswith('_')])
print("fetch_carrier_status available:", hasattr(switching, 'fetch_carrier_status'))
print("select_best_carrier available:", hasattr(switching, 'select_best_carrier'))
print("switch_network available:", hasattr(switching, 'switch_network'))
