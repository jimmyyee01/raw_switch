import sys
sys.path.insert(0, 'C:/Users/MAMA TECH SOLUTIONS')

import sim_aggregator.config as cfg
print('config import OK')
print('CARRIER_A_KEY:', cfg.CARRIER_A_KEY)
print('API_KEY:', cfg.API_KEY)
print('MIN_BANDWIDTH_MBPS:', cfg.MIN_BANDWIDTH_MBPS)
print('MAX_LATENCY_MS:', cfg.MAX_LATENCY_MS)
