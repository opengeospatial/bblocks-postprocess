const jsonld = require('jsonld');
const util = require('util');
const fs = require('fs');

if (process.argv.length !== 3) {
    console.error('File name to validate required');
    process.exit(25);
}

const filename = process.argv[2];
const readFile = util.promisify(fs.readFile);
readFile(filename, 'utf-8')
    .then(data => JSON.parse(data))
    .then(context => jsonld.toRDF(context, {format: 'application/n-quads'}))
    .catch(e => {
        if (e?.details?.code) {
            console.log(`${e.message} (${e.details.code})`);
        } else {
            console.log(e.message);
        }
        process.exit(26);
    });

