import sys
sys.path.insert(0, 'C:/Users/MAMA TECH SOLUTIONS')
from sim_aggregator.main import app
print("app:", app)
print("routes:", len(app.routes))
for route in app.routes:
    print(" -", getattr(route, "path", str(route)))
