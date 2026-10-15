"""Compatibility for the historical explicit unittest command.

Default discovery uses test_pipeline.py; this file is not a second test suite.
"""
from test_pipeline import PipelineTests

if __name__ == '__main__':
    import unittest
    unittest.main()
