import {test,expect} from '@playwright/test';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {once} from 'node:events';
import {fileURLToPath} from 'node:url';
let child,fixture;
test.beforeAll(async()=>{
 child=spawn(process.env.COURSE_WEB_PYTHON || 'python3',['-u',fileURLToPath(new URL('../scripts/web_browser_fixture.py',import.meta.url))],{stdio:['ignore','pipe','pipe']});
 child.stderr.on('data',b=>process.stderr.write(b));
 const lines=createInterface({input:child.stdout});
 fixture=JSON.parse((await once(lines,'line'))[0]);lines.close();
});
test.afterAll(async()=>{if(child && child.exitCode===null){child.kill('SIGTERM');await once(child,'exit');}});
async function connect(page){
 await page.goto(`${fixture.site}/#token=${fixture.token}&port=${fixture.port}`);
 await expect(page.getByText('● Mac conectado')).toBeVisible();
 await expect(page.getByRole('button',{name:/Curso de exemplo/})).toBeVisible();
 await expect(page).not.toHaveURL(/token=/);
}
test('published UI connects to local CLI and completes a real dry run',async({page})=>{
 await connect(page);
 await page.getByRole('button',{name:/Verificar curso/}).click();
 await expect(page.getByRole('heading',{name:'Operação concluída.'})).toBeVisible();
 await expect(page.getByText(/Verificação: 3 aulas/)).toBeVisible();
 await expect(page.getByText(/3 pendentes/)).toBeVisible();
 const screenshot=process.env.COURSE_WEB_SCREENSHOT;
 if(screenshot)await page.screenshot({path:screenshot,fullPage:true});
});
test('delivery delegates to the CLI and cancellation uses its exact owned identity',async({page})=>{
 await connect(page);
 await page.locator('#operation').selectOption('deliver');
 await page.getByRole('button',{name:/Iniciar entrega/}).click();
 await expect(page.getByText(/Aguardando confirmação do iCloud · Example lesson/)).toBeVisible();
 await expect(page.getByText('1 de 3 aulas entregues')).toBeVisible();
 await expect(page.getByRole('button',{name:/Iniciar entrega/})).toBeDisabled();
 await expect(page.getByText(/Aguardando confirmação do iCloud · Example lesson/)).toBeVisible();
 await page.getByRole('button',{name:'Cancelar com segurança'}).click();
 await expect(page.getByRole('heading',{name:'Operação cancelada.'})).toBeVisible();
});
test('invalid pairing stays disconnected and mobile layout has no horizontal overflow',async({page})=>{
 await page.setViewportSize({width:390,height:844});
 await page.goto(`${fixture.site}/#token=wrong&port=${fixture.port}`);
 await expect(page.getByText(/A chave expirou ou está incorreta/)).toBeVisible();
 await expect(page.getByRole('button',{name:/Verificar curso/})).toBeDisabled();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await connect(page);
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBe(true);
 await page.getByRole('button',{name:'Desconectar painel'}).click();
 await expect(page.getByText(/Painel desconectado/)).toBeVisible();
});
test('a delayed old poll cannot replace the current CLI run',async({page})=>{
 await page.addInitScript(()=>{
  const nativeFetch=window.fetch.bind(window);
  window.fetch=async(...args)=>{
   const response=await nativeFetch(...args);
   if(response.headers.get('x-test-stale')==='yes'){
    const json=response.json.bind(response);
    response.json=async()=>{const value=await json();window.oldPollParsed=true;return value;};
   }
   return response;
  };
 });
 await connect(page);
 let captured,release;
 const held=new Promise(resolve=>{captured=resolve;});
 const gate=new Promise(resolve=>{release=resolve;});
 let first=true;
 await page.route('**/api/state',async route=>{
  if(!first){await route.continue();return;}
  first=false;
  const response=await route.fetch();captured();await gate;
  await route.fulfill({response,headers:{...response.headers(),'x-test-stale':'yes','access-control-expose-headers':'x-test-stale'}});
 });
 await held;
 await page.locator('#operation').selectOption('deliver');
 await page.getByRole('button',{name:/Iniciar entrega/}).click();
 await expect(page.getByRole('heading',{name:'Seu CLI está trabalhando.'})).toBeVisible();
 const delivered=page.waitForResponse(response=>response.headers()['x-test-stale']==='yes');
 release();await delivered;
 await page.waitForFunction(()=>window.oldPollParsed===true);
 await expect(page.getByRole('heading',{name:'Seu CLI está trabalhando.'})).toBeVisible();
 await expect(page.getByRole('button',{name:'Cancelar com segurança'})).toBeVisible();
 await expect(page.getByText(/Aguardando confirmação do iCloud · Example lesson/)).toBeVisible();
 await page.getByRole('button',{name:'Cancelar com segurança'}).click();
 await expect(page.getByRole('heading',{name:'Operação cancelada.'})).toBeVisible();
});
