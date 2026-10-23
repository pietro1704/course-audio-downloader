import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'.',testMatch:'browser.spec.mjs',workers:1,timeout:30000,
 use:{headless:true,launchOptions:{args:['--disable-gpu']}},reporter:'list'});
