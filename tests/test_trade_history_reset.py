import unittest
from backend.trade_history import fill_timestamp,since_reset
class ResetTests(unittest.TestCase):
 def test_reset_preserves_only_new_fills_across_timestamp_formats(self):
  cutoff=1792920000
  rows=[{'created_at':'2026-10-25T09:20:00Z','id':'boundary'},{'created_at':cutoff-1,'id':'old'},{'created_at':cutoff+1,'id':'seconds'},{'created_at':(cutoff+2)*1000,'id':'milliseconds'},{'created_at':(cutoff+3)*1000000,'id':'microseconds'},{'created_at':'2026-10-25T09:20:04+00:00','id':'iso'},{'id':'unknown'}]
  self.assertEqual([r['id'] for r in since_reset(rows,cutoff)],['seconds','milliseconds','microseconds','iso'])
 def test_no_reset_leaves_exchange_history_intact(self):
  rows=[{'id':1}];self.assertIs(since_reset(rows,None),rows)
 def test_invalid_timestamps_cannot_reintroduce_history(self):
  for value in [None,'bad',float('nan'),float('inf'),'']:
   self.assertIsNone(fill_timestamp(value))
