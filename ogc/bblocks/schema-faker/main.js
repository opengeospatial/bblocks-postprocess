const jsf = require('json-schema-faker');
const yaml = require('js-yaml');
const fs = require('fs');

const schemaContents = fs.readFileSync(process.stdin.fd, 'utf-8');
const schema = yaml.load(schemaContents);
const fake = jsf.generate(schema);
console.log(JSON.stringify(fake));