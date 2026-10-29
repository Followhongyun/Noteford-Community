'use strict';

// Off-screen drawing only. Uses public QuartzCore/CoreGraphics APIs; no window,
// screen capture, accessibility access, account data or additional permissions.
function createRenderer() {
  const koffi = require('koffi');
  const objc = koffi.load('/usr/lib/libobjc.A.dylib');
  const cf = koffi.load('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation');
  const cg = koffi.load('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics');
  const io = koffi.load('/System/Library/Frameworks/ImageIO.framework/ImageIO');
  koffi.load('/System/Library/Frameworks/QuartzCore.framework/QuartzCore');
  const point = koffi.struct('NFCornerPoint', {x: 'double', y: 'double'});
  const size = koffi.struct('NFCornerSize', {width: 'double', height: 'double'});
  const rect = koffi.struct('NFCornerRect', {origin: point, size});
  const getClass = objc.func('void *objc_getClass(const char *)');
  const selector = objc.func('void *sel_registerName(const char *)');
  const send = objc.func('objc_msgSend', 'void *', ['void *', 'void *']);
  const setPointer = objc.func('objc_msgSend', 'void', ['void *', 'void *', 'void *']);
  const setNumber = objc.func('objc_msgSend', 'void', ['void *', 'void *', 'double']);
  const setInteger = objc.func('objc_msgSend', 'void', ['void *', 'void *', 'uint64_t']);
  const setRect = objc.func('objc_msgSend', 'void', ['void *', 'void *', rect]);
  const poolPush = objc.func('void *objc_autoreleasePoolPush(void)');
  const poolPop = objc.func('void objc_autoreleasePoolPop(void *)');
  const release = cf.func('void CFRelease(void *)');
  const string = cf.func('void *CFStringCreateWithCString(void *, const char *, uint32_t)');
  const dataCreate = cf.func('void *CFDataCreateMutable(void *, long)');
  const dataLength = cf.func('long CFDataGetLength(void *)');
  const dataBytes = cf.func('void *CFDataGetBytePtr(void *)');
  const colorSpace = cg.func('void *CGColorSpaceCreateDeviceRGB(void)');
  const color = cg.func('void *CGColorCreateGenericRGB(double, double, double, double)');
  const bitmap = cg.func('void *CGBitmapContextCreate(void *, size_t, size_t, size_t, size_t, void *, uint32_t)');
  const scaleContext = cg.func('void CGContextScaleCTM(void *, double, double)');
  const makeImage = cg.func('void *CGBitmapContextCreateImage(void *)');
  const destination = io.func('void *CGImageDestinationCreateWithData(void *, void *, size_t, void *)');
  const addImage = io.func('void CGImageDestinationAddImage(void *, void *, void *)');
  const finalize = io.func('bool CGImageDestinationFinalize(void *)');
  const sel = Object.fromEntries(['layer', 'setBounds:', 'setCornerRadius:', 'setCornerCurve:', 'setMaskedCorners:', 'setContentsScale:', 'setBackgroundColor:', 'setBorderWidth:', 'setBorderColor:', 'renderInContext:'].map(name => [name, selector(name)]));
  const layerClass = getClass('CALayer');
  if (!layerClass) throw Error('CALayer unavailable');

  return function render({width, height, radius, corners, scale, stroke}, curve = 'continuous') {
    const pool = poolPush(), owned = [];
    const own = value => { if (!value) throw Error('Native corner allocation failed'); owned.push(value); return value; };
    try {
      const layer = send(layerClass, sel.layer);
      setRect(layer, sel['setBounds:'], {origin: {x: 0, y: 0}, size: {width, height}});
      setNumber(layer, sel['setCornerRadius:'], radius);
      setPointer(layer, sel['setCornerCurve:'], own(string(null, curve, 0x08000100)));
      setInteger(layer, sel['setMaskedCorners:'], corners);
      setNumber(layer, sel['setContentsScale:'], scale);
      const white = own(color(1, 1, 1, 1));
      if (stroke) {
        setNumber(layer, sel['setBorderWidth:'], stroke);
        setPointer(layer, sel['setBorderColor:'], white);
      } else setPointer(layer, sel['setBackgroundColor:'], white);
      const w = Math.ceil(width * scale), h = Math.ceil(height * scale);
      const context = own(bitmap(null, w, h, 8, w * 4, own(colorSpace()), 1));
      scaleContext(context, scale, scale);
      setPointer(layer, sel['renderInContext:'], context);
      const image = own(makeImage(context));
      const data = own(dataCreate(null, 0));
      const dest = own(destination(data, own(string(null, 'public.png', 0x08000100)), 1, null));
      addImage(dest, image, null);
      if (!finalize(dest)) throw Error('Native corner encoding failed');
      const bytes = Buffer.from(koffi.decode(dataBytes(data), 'uint8_t', dataLength(data)));
      return 'data:image/png;base64,' + bytes.toString('base64');
    } finally {
      for (const value of owned.reverse()) release(value);
      poolPop(pool);
    }
  };
}

module.exports = {createRenderer};
