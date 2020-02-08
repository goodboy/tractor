"""
Needed in windows.
This needs to be the main program as it will be
called '__mp_main__' by the multiprocessing module

"""
if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    from . import test_example
    test_example.tractor.run(test_example.main, start_method='spawn')
