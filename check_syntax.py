import ast
files = ["myclip/ui.py"]
for f in files:
    try:
        ast.parse(open(f, encoding="utf-8").read())
        print(f"{f}: OK")
    except SyntaxError as e:
        print(f"{f}: SYNTAX ERROR - {e}")
        raise
print("All syntax OK")
