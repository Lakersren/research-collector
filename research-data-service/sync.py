#!/usr/bin/env python3
"""One-shot pull/import job, called by the daily systemd timer or manually."""
import bootstrap
import logging
import uuid
from aris.sync import Puller
from aris.observability import setup_logging,log_context

if __name__=='__main__':
    setup_logging('sync')
    with log_context(job_id=str(uuid.uuid4()),trigger='sync'):
        try:
            Puller().run()
        except Exception:
            logging.getLogger('corpus.sync').exception('sync_job_failed')
            raise SystemExit(1)
