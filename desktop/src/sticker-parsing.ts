import { createContext } from 'react'

// Do not mount sticker media until the current account's preference is known.
export const StickerParsingContext = createContext(false)
