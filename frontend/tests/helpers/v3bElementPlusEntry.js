// element-plus as the app entry sees it under helpers/v3bAppEntry.js: the
// recording stand-in of helpers/elementPlus.js (the same module instance every
// other src module gets), plus the library's default export, the plugin
// main.js installs with app.use().
export * from './elementPlus.js'
export { default } from 'element-plus'
