First generate a built disti:

```
python -m pip install --upgrade build 
python -m build --sdist --outdir dist/alpha5/
```

Then try a test ``pypi`` upload:

```
python -m twine upload --repository testpypi dist/alpha5/*
```

The push to `pypi` for realz.

```
python -m twine upload --repository testpypi dist/alpha5/*
```
