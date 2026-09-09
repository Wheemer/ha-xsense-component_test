"""Isolated Chromium regression checks for the shipped recordings panel.

Run with Playwright and its Chromium browser installed. No HA or media access.
"""

from pathlib import Path

from playwright.sync_api import sync_playwright

from force_arm_browser_checks import check_force_arm_panel


ROOT = Path(__file__).resolve().parents[1]


def check_paused_timezone_transition(context):
    page = context.new_page()
    page.set_content("<body></body>")
    page.add_script_tag(path=str(ROOT / "custom_components/xsense/frontend/recordings-panel.js"))
    result = page.evaluate("""async () => {
      const event = (target,name) => new Promise((resolve,reject)=>{
        const timeout=setTimeout(()=>reject(new Error(`Timed out waiting for ${name}`)),10000);
        target.addEventListener(name,()=>{clearTimeout(timeout);resolve()},{once:true});
      });
      const canvas=document.createElement('canvas');canvas.width=640;canvas.height=360;
      const paint=canvas.getContext('2d');const stream=canvas.captureStream(20);const chunks=[];
      const recorder=new MediaRecorder(stream,{mimeType:'video/webm'});
      recorder.ondataavailable=e=>chunks.push(e.data);
      recorder.start();let frame=0;
      const drawing=setInterval(()=>{paint.fillStyle=frame++%2?'#285c3c':'#505050';paint.fillRect(0,0,640,360)},50);
      await new Promise(resolve=>setTimeout(resolve,1600));
      const stopped=event(recorder,'stop');recorder.stop();await stopped;
      clearInterval(drawing);stream.getTracks().forEach(track=>track.stop());
      const url=URL.createObjectURL(new Blob(chunks,{type:'video/webm'}));
      const panel=document.createElement('xsense-recordings-panel');
      panel.logPanelEvent=()=>{};
      let finishThumbnails;
      panel.signVisibleThumbnails=()=>new Promise(resolve=>{finishThumbnails=resolve});
      const clip={entry_id:'entry',serial:'camera',start:1788913800,end:1788913810,date:'2026-09-09',duration:10};
      panel.data={cameras:[{entry_id:'entry',serial:'camera',name:'Camera',dates:[clip.date],clips:[clip]}]};
      panel._hass={config:{time_zone:'UTC'},locale:{language:'en',time_format:'24',time_zone:'server'},callApi:async()=>({})};
      history.replaceState(null,'',`#entry_id=entry&serial=camera&start=${clip.start}&end=${clip.end}`);
      document.body.append(panel);
      panel.playbackUrls.set(panel.playbackKey(clip),url);panel.playbackTypes.set(panel.playbackKey(clip),'blob');
      panel.render();
      const original=panel.shadowRoot.getElementById('viewer-video');
      if(original.readyState<2)await event(original,'loadeddata');
      original.pause();const seeked=event(original,'seeked');original.currentTime=0.8;await seeked;
      const before={time:original.currentTime,paused:original.paused};
      let destroyed=0;const hls={destroy:()=>{destroyed++}};
      const key=panel.playbackKey(clip);
      panel.hlsInstances.set(key,hls);panel.playbackTokens.set(key,{clip,token:'synthetic-token'});
      const snapshot=()=>{const current=panel.shadowRoot.getElementById('viewer-video');return {
        sameVideo:original===current,time:current.currentTime,paused:current.paused,
        title:panel.shadowRoot.getElementById('viewer-time').textContent,
        date:panel.selectedDate,tokens:panel.playbackTokens.size,urls:panel.playbackUrls.size,
        sameHls:panel.hlsInstances.get(key)===hls,destroyed,
        back:panel.shadowRoot.getElementById('back').textContent,
        deleteLabel:panel.shadowRoot.getElementById('delete-viewer').getAttribute('aria-label'),
      }};
      panel.hass={...panel._hass,config:{time_zone:'America/St_Johns'}};
      const immediate=snapshot();finishThumbnails();
      await new Promise(resolve=>setTimeout(resolve,100));
      const settled=snapshot();
      panel.hass={...panel._hass,language:'fr',locale:{...panel._hass.locale,language:'fr',time_format:'12'}};
      const localeImmediate=snapshot();
      await new Promise(resolve=>setTimeout(resolve,100));
      const localeSettled=snapshot();
      const expected={back:panel.t('back'),deleteLabel:panel.t('deleteCachedRecording'),time:panel.formatClipTime(clip)};
      panel.signVisibleThumbnails=async()=>{};
      let finishReload;
      panel._hass.callApi=()=>new Promise(resolve=>{finishReload=resolve});
      const reload=panel.loadData();const reloadImmediate=snapshot();
      finishReload(structuredClone(panel.data));await reload;
      const reloadSettled=snapshot();
      panel._hass.callApi=async()=>{throw new Error('synthetic refresh failure')};
      await panel.loadData();const failedReload=snapshot();
      const refreshError=panel.shadowRoot.querySelector('.error')?.textContent;
      await panel.handleRouteChange();const sameRoute=snapshot();
      panel.remove();
      return {before,immediate,settled,localeImmediate,localeSettled,expected,
        reloadImmediate,reloadSettled,failedReload,sameRoute,refreshError};
    }""")
    assert result["before"]["paused"] is True, result
    assert result["refreshError"] == "synthetic refresh failure", result
    for stage in ("immediate", "settled", "localeImmediate", "localeSettled",
                  "reloadImmediate", "reloadSettled", "failedReload", "sameRoute"):
        current = result[stage]
        assert current["sameVideo"] is True, result
        assert current["paused"] is True, result
        assert abs(current["time"] - result["before"]["time"]) < 0.01, result
        assert current["date"] == "2026-09-08", result
        assert current["urls"] == 1, result
        assert current["tokens"] == 1, result
        assert current["sameHls"] is True and current["destroyed"] == 0, result
        if stage not in ("immediate", "settled"):
            assert current["back"] == result["expected"]["back"] == "Retour", result
            assert current["deleteLabel"] == result["expected"]["deleteLabel"], result
            assert result["expected"]["time"] in current["title"], result
        else:
            assert "22:00:00" in current["title"], result
    page.close()


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(timezone_id="America/Los_Angeles", service_workers="block")
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.set_content("""<style>
          body{margin:0;--primary-text-color:#202020;--primary-background-color:#fafafa;
          --secondary-text-color:#606060;--card-background-color:#fff;--divider-color:#ddd}
        </style>""")
        page.add_script_tag(path=str(ROOT / "custom_components/xsense/frontend/recordings-panel.js"))
        page.evaluate("""() => {
          customElements.define('ha-icon', class extends HTMLElement {
            connectedCallback() { this.style.cssText='display:inline-block;width:24px;height:24px;flex-shrink:0'; }
          });
          const panel = document.createElement('xsense-recordings-panel');
          window.panel = panel;
          const clips = [0,1,2].map(i => ({entry_id:'entry-a',serial:'cam-a',
            start:1788940800+i*60,end:1788940810+i*60,duration:10,
            date:'2026-09-09',cached:true,cache_bytes:2345678}));
          panel.data = {cameras:[{entry_id:'entry-a',serial:'cam-a',name:'Garden camera',
            dates:['2026-09-09'],clips}],stats:{ready_clips:3,total_bytes:7037034,
            total_cameras:1,online_cameras:1}};
          panel.selectedCameraKey='entry-a:cam-a';panel.selectedDate='2026-09-09';
          panel._hass={language:'en',locale:{language:'en',time_format:'24',time_zone:'server'},
            config:{time_zone:'UTC'},callApi:async()=>({})};
          const canvas=document.createElement('canvas');canvas.width=640;canvas.height=360;
          const paint=canvas.getContext('2d');paint.fillStyle='#285c3c';paint.fillRect(0,0,640,360);
          window.syntheticVideo=canvas.captureStream(1);
          const afterRender=panel.afterRender.bind(panel);
          panel.afterRender=()=>{
            if(!window.syntheticVideoState){afterRender();return;}
            const video=panel.shadowRoot.getElementById('viewer-video');
            video.removeAttribute('src');video.muted=true;video.srcObject=window.syntheticVideo;
            video.play().catch(()=>{});
          };
          document.body.append(panel);
        }""")
        languages = page.evaluate("Object.keys(TRANSLATIONS)")
        assert len(languages) == 38, f"Update locale coverage expectation: {languages}"
        failures = []
        cases = 0
        for language in languages:
            for width in (1440, 768, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                for state in ("list", "preparing", "playback-error", "empty", "video"):
                    page.evaluate("""({language,state}) => {
                      panel._hass.language=language;panel._hass.locale.language=language;
                      panel.selectedClip=['preparing','playback-error','video'].includes(state)
                        ? panel.data.cameras[0].clips[0] : null;
                      panel.selectedDate=state==='empty'?'2026-09-08':'2026-09-09';
                      panel.playbackErrors.clear();panel.playbackLoadingKey='';
                      panel.playbackUrls.clear();panel.playbackTypes.clear();
                      window.syntheticVideoState=state==='video';
                      if(state==='video')panel.playbackUrls.set(panel.playbackKey(panel.selectedClip),'#synthetic-video');
                      if(state==='preparing')panel.playbackLoadingKey=panel.playbackKey(panel.selectedClip);
                      if(state==='playback-error')panel.playbackErrors.set(
                        panel.playbackKey(panel.selectedClip),panel.t('hlsPlaybackUnsupported'));
                      panel.render();
                    }""", {"language": language, "state": state})
                    if state == "video":
                        page.wait_for_function("panel.shadowRoot.querySelector('video')?.videoWidth === 640")
                    layout = page.evaluate("""() => ({
                      width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
                      video:(()=>{const video=panel.shadowRoot.querySelector('video');if(!video)return null;
                        const frame=video.parentElement.getBoundingClientRect(),rect=video.getBoundingClientRect();
                        return {left:rect.left,right:rect.right,frameLeft:frame.left,frameRight:frame.right,
                          width:rect.width,height:rect.height};})(),
                      overflow:[...panel.shadowRoot.querySelectorAll('button,select,.title,.stat-value,.no-video,video')]
                        .filter(e=>e.scrollWidth>e.clientWidth+2)
                        .map(e=>({tag:e.tagName,text:e.textContent.trim(),client:e.clientWidth,scroll:e.scrollWidth}))
                    })""")
                    cases += 1
                    if layout["scrollWidth"] > width + 1 or layout["overflow"]:
                        failures.append((language, width, state, layout))
                    if state == "video":
                        video = layout["video"]
                        if (video["left"] < video["frameLeft"] - 1
                                or video["right"] > video["frameRight"] + 1
                                or video["right"] > width + 1
                                or abs(video["height"] - video["width"] * 9 / 16) > 1):
                            failures.append((language, width, state, video))
        assert not failures, f"Layout failures ({len(failures)}/{cases}): {failures[:10]}"

        page.evaluate("""() => {
          panel.selectedClip=null;panel.selectedDate='2026-09-09';
          window.syntheticVideoState=false;panel.playbackUrls.clear();
          panel._hass.language='en';panel._hass.locale.language='en';
          window.deleted=0;window.opened=0;
          panel.deleteClipCache=async()=>{window.deleted++};
          panel.openClip=async()=>{window.opened++};panel.render();
        }""")
        delete = page.locator(".clip-delete").first
        delete.focus()
        page.keyboard.press("Enter")
        assert page.evaluate("window.deleted") == 1, "Enter must activate nested delete"
        page.keyboard.press("Space")
        assert page.evaluate("window.deleted") == 2, "Space must activate nested delete"
        assert page.evaluate("window.opened") == 0, "Delete must not also open playback"
        delete.click()
        assert page.evaluate("window.deleted") == 3
        card = page.locator(".clip").first
        card.focus()
        page.keyboard.press("Enter")
        page.keyboard.press("Space")
        assert page.evaluate("window.opened") == 2, "Card keyboard activation must remain usable"

        # Use a browser timezone different from HA, including a UTC day boundary.
        timezone_result = page.evaluate("""() => {
          const sample={start:Date.parse('2026-09-09T00:30:00Z')/1000,
            end:Date.parse('2026-09-09T00:30:10Z')/1000,date:'2026-09-09'};
          const data={cameras:[{dates:['2026-09-09'],clips:[sample]}]};
          panel._hass.config.time_zone='America/St_Johns';panel._hass.locale.time_zone='server';
          const server={time:panel.formatTime(sample.start),date:panel.groupPanelDates(data).cameras[0].dates[0]};
          panel._hass.locale.time_zone='local';
          const local={time:panel.formatTime(sample.start),date:panel.groupPanelDates(data).cameras[0].dates[0]};
          return {server,local,original:sample};
        }""")
        assert timezone_result["server"] == {"time": "22:00:00", "date": "2026-09-08"}, timezone_result
        assert timezone_result["local"] == {"time": "17:30:00", "date": "2026-09-08"}, timezone_result
        assert timezone_result["original"]["date"] == "2026-09-09"
        page.evaluate("window.syntheticVideo.getTracks().forEach(track=>track.stop())")
        check_paused_timezone_transition(context)
        force_arm_cases = check_force_arm_panel(context, ROOT)
        print(f"Passed {force_arm_cases} force-arm layout cases across all 41 backend locales")
        print(f"Passed {cases} layout cases ({len(languages)} locales), Enter/Space controls, server/local timezone grouping, and paused-media timezone/locale/reload/route transitions")
        browser.close()


if __name__ == "__main__":
    main()
