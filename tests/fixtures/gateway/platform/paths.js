const path=require('node:path');
module.exports={AUTH_LOCK:path.join(process.env.MADMODEL_STATE_DIR,'auth.lock')};
