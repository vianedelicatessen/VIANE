"""
Worker independiente (para Render / Railway / Fly / un VPS): procesa la cola de trabajos en bucle.
    python worker.py            # corre para siempre
    python worker.py --once     # procesa lo pendiente y termina
No hace falta si usas QStash o el cron de Vercel: /api/worker/run hace lo mismo.
"""
import sys
import time

import app as A
import infra

if __name__ == '__main__':
    once = '--once' in sys.argv
    while True:
        res = infra.run_jobs(A.db, limit=20, budget=40.0)
        if res['reclamados']:
            print(f'[worker] {res}')
        if once:
            break
        time.sleep(2 if res['reclamados'] else 5)
