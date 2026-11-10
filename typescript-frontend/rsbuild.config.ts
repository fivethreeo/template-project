import { defineConfig } from '@rsbuild/core';
import { pluginSass } from '@rsbuild/plugin-sass';
import { pluginDjangoReload } from './scripts/django-reload.mjs';

const assetPrefix = process.env.RSBUILD_BASE_URL;
const corsOrigins = (process.env.RSBUILD_CORS_ORIGIN || '')
  .split(',')
  .map((origin) => origin.trim())
  .filter(Boolean);
const hmrHost = process.env.RSBUILD_HMR_HOST;

// Docs: https://rsbuild.rs/config/
export default defineConfig(({ env }) => {
  const isDevelopment = env === 'development';

  return {
  plugins: [pluginSass(), pluginDjangoReload()],
  dev: {
    // Django serves the HTML, so hot-update assets must keep the Rsbuild origin.
    assetPrefix: assetPrefix || true,
    client: {
      ...(hmrHost ? { host: hmrHost } : {}),
      port: '<port>',
    },
    // Django reads the development manifests from disk.
    writeToDisk: true,
  },
  server: {
    port: 3000,
    strictPort: true,
    cors: corsOrigins.length ? { origin: corsOrigins } : false,
    headers: {
      'Cache-Control': 'no-store',
    },
  },
  output: {
    manifest: true,
    filenameHash: !isDevelopment,
    distPath: {
      js: 'js',
      jsAsync: 'js',
      css: 'css',
      cssAsync: 'css',
      svg: 'assets',
      font: 'assets',
      wasm: 'assets',
      image: 'assets',
      media: 'assets',
      assets: 'assets',
    },
    ...(!isDevelopment && process.env.RSBUILD_ASSET_PREFIX
      ? { assetPrefix: process.env.RSBUILD_ASSET_PREFIX }
      : {}),
  },
  html: {
    template: './src/index.html',
  },
  splitChunks: isDevelopment ? false : undefined,
  source: {
    entry: {
      index: './src/index.ts',
    },
  },
  tools: {
    cssExtract: {
      loaderOptions: { publicPath: '../' },
    },
    rspack: {
      output: {
        uniqueName: 'template_app',
        ...(isDevelopment ? { chunkFilename: 'js/[name].js' } : {}),
      },
      optimization: {
        runtimeChunk: 'single',
      },
    },
  },
  };
});
