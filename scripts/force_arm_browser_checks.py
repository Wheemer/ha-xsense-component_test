"""Render force-arm translations in Chromium without calling HA services."""

import json


def check_force_arm_panel(context, root):
    page = context.new_page()
    page.set_content("<body style='margin:0'></body>")
    page.add_script_tag(path=str(root / "custom_components/xsense/frontend/force-arm-panel.js"))
    cases = 0
    for path in sorted((root / "custom_components/xsense/translations").glob("*.json")):
        localized = json.loads(path.read_text(encoding="utf-8"))
        page.evaluate("""async ({language, localized}) => {
          window.forcePanel?.remove();
          const resources={};
          const flatten=(data,prefix)=>{
            for(const [key,value] of Object.entries(data)) {
              const full=prefix+'.'+key;
              if(typeof value==='string')resources[full]=value;
              else flatten(value,full);
            }
          };
          flatten(localized,'component.xsense');
          const panel=document.createElement('xsense-force-arm-panel');
          panel._started=true;
          panel._mode='Home';
          panel._hass={language,callWS:async()=>({resources}),
            callService:()=>{throw new Error('No services allowed in browser fixture')},
            localize:()=> 'Back'};
          panel._language=language;
          document.body.append(panel);
          await panel.loadTranslations(language);
          window.forcePanel=panel;
        }""", {"language": path.stem, "localized": localized})
        for width in (1440, 768, 390, 320):
            page.set_viewport_size({"width": width, "height": 900})
            for state in ("pending", "submitted", "invalid", "failed"):
                result = page.evaluate("""({state}) => {
                  const panel=window.forcePanel;
                  panel._status=state;
                  panel._error={translation_domain:'xsense',translation_key:'force_arm_request_failed'};
                  panel.render();
                  const nodes=[...panel.querySelectorAll('main,h1,p,button')];
                  return {
                    title:panel.querySelector('h1').textContent,
                    message:panel.querySelector('p').textContent,
                    expected:state==='invalid'?panel.text('exceptions.force_arm_invalid_link.message'):
                      state==='failed'?panel.text('exceptions.force_arm_request_failed.message'):null,
                    overflow:nodes.filter(node=>{
                      const r=node.getBoundingClientRect();
                      return r.left < -1 || r.right > innerWidth+1 || node.scrollWidth>node.clientWidth+1;
                    }).map(node=>node.tagName),
                    documentWidth:document.documentElement.scrollWidth,
                    back:!!panel.querySelector('button'),
                  };
                }""", {"state": state})
                label = (path.stem, width, state, result)
                assert result["title"] == localized["services"]["force_arm"]["name"], label
                assert result["message"], label
                if result["expected"] is not None:
                    assert result["message"] == result["expected"], label
                assert result["back"] == (state in ("invalid", "failed")), label
                assert not result["overflow"] and result["documentWidth"] <= width, label
                cases += 1
    page.close()
    return cases
