declare module 'jmuxer' {
  export interface JMuxerOptions {
    node: HTMLCanvasElement
    mode: 'video' | 'audio' | 'both'
    fps?: number
    debug?: boolean
    flushingTime?: number
    clearBuffer?: boolean
  }

  export interface JMuxerFeedData {
    video?: Uint8Array
    audio?: Uint8Array
  }

  export default class JMuxer {
    constructor(options: JMuxerOptions)
    feed(data: JMuxerFeedData): void
    destroy(): void
  }
}
