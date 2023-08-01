import jsonschema
from ogc.na.util import load_yaml

d = load_yaml(filename='/home/alx5000/work/Proyectos/ogc/3d-csdm-schema/_sources/csdm/test/example.json')
schema = load_yaml(filename='/home/alx5000/work/Proyectos/ogc/3d-csdm-schema/build-local/annotated/csdm/test/schema.json')
jsonschema.validate(d, schema)
