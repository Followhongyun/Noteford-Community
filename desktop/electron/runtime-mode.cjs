'use strict';
function runtimeMode({packaged,argv}) {
  const development=argv.includes('--dev'),localLive=argv.includes('--local-live');
  if(packaged&&(development||localLive))throw Error('发布版不接受源码开发参数，请使用正式登录入口。');
  return {development:!packaged&&development,localLive:!packaged&&localLive};
}
module.exports={runtimeMode};
