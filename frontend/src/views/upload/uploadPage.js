// The provide/inject key under which views/Upload.vue hands its ctx (every name its parts define) to the
// components that render parts of the page; each of them injects it and takes the names its template uses.
export const UPLOAD_PAGE = Symbol('upload page')
