import { defineConfig } from 'vitepress'

const repo = 'cities-skylines-ai-agent'
const base = `/${repo}/`

export default defineConfig({
  title: 'Cities: Skylines AI Agent',
  description: 'An AI mayor that plays Cities: Skylines 1 through a local API bridge mod.',
  base,
  cleanUrls: true,
  head: [
    ['meta', { property: 'og:title', content: 'Cities: Skylines AI Agent' }],
    ['meta', { property: 'og:description', content: 'Operate Cities: Skylines 1 through a local API bridge built for AI agents.' }]
  ],
  themeConfig: {
    siteTitle: 'CS1 AI Agent',
    nav: [
      { text: 'Guide', link: '/guide/getting-started' },
      { text: 'API', link: '/api' },
      { text: 'GitHub', link: 'https://github.com/ChenShunHsu/cities-skylines-ai-agent' }
    ],
    sidebar: [
      {
        text: 'Guide',
        items: [
          { text: 'Getting Started', link: '/guide/getting-started' },
          { text: 'Agent Workflow', link: '/guide/usage' },
          { text: 'Autonomous Play Loop', link: '/guide/autonomous-loop' },
          { text: 'Compact Mayor', link: '/guide/compact-mayor' },
          { text: 'Architecture', link: '/guide/architecture' },
          { text: 'Troubleshooting', link: '/guide/troubleshooting' }
        ]
      },
      {
        text: 'Reference',
        items: [
          { text: 'API Reference', link: '/api' },
          { text: 'Base Game API Coverage', link: '/base-game-api-coverage' }
        ]
      }
    ],
    socialLinks: [
      { icon: 'github', link: 'https://github.com/ChenShunHsu/cities-skylines-ai-agent' }
    ],
    footer: {
      message: 'Released under the MIT License.',
      copyright: 'Cities: Skylines is a trademark of Colossal Order / Paradox Interactive. This project is an independent experimental bridge.'
    }
  }
})
