import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.path.dirname(HERE)  # workspace 根（db.py / supabase_client.py 所在）
if SHARED not in sys.path:
    sys.path.insert(0, SHARED)
