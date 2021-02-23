from importlib import import_module
from inspect import isclass
from pathlib import Path
from pkgutil import iter_modules

# Automatically load all modules in the `cvedb` package,
# so all Feeds will auto-register themselves:
package_dir = Path(__file__).resolve().parent
for (_, module_name, _) in iter_modules([str(package_dir)]):
    if module_name == "__main__":
        continue
    # import the module and iterate through its attributes
    module = import_module(f"{__name__}.{module_name}")
    for attribute_name in dir(module):
        attribute = getattr(module, attribute_name)

        if isclass(attribute):
            # Add the class to this package's variables
            globals()[attribute_name] = attribute
