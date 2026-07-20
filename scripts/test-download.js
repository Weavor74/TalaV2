/**
 * test-download.js - Auto-generated documentation base.
 */

const https = require('https');
const fs = require('fs');
const os = require('os');
const path = require('path');
const zipPath = path.join(os.tmpdir(), 'tala-test-download.zip');
const file = fs.createWriteStream(zipPath);

https.get('https://talacore.app/tala-windows.zip', (response) => {
    console.log('Status:', response.statusCode);
    if (response.statusCode === 200) {
        response.pipe(file);
        file.on('finish', () => {
            console.log('Downloaded to', zipPath);
            fs.stat(zipPath, (err, stats) => {
                console.log('File size:', stats.size);
            });
        });
    } else {
        console.error('Failed to download');
    }
}).on('error', (e) => {
    console.error('Error:', e);
});
