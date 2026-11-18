import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'
import LocatorLedgerView from '@/views/LocatorLedgerView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 定位策略执行台账（固定路径，必须先于 /:moduleKey 动态路由）
    { path: '/locator-ledger', name: 'locator-ledger', component: LocatorLedgerView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
