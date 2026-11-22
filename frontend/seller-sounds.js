/* MYRAA original UI tones. OFF by default; never autoplay or disturb screen readers. */
(() => {
 'use strict';
 const key='myraaSellerSoundEnabled';
 let enabled=localStorage.getItem(key)==='1';
 let ctx=null;
 const button=document.getElementById('sound-toggle');
 function draw(){if(!button)return;button.setAttribute('aria-pressed',String(enabled));button.innerHTML=enabled?'<i class="fa-solid fa-volume-high"></i> Sound on':'<i class="fa-solid fa-volume-xmark"></i> Sound off';button.title=enabled?'Disable sounds and scan speech':'Enable sounds and scan speech';}
 function tone(frequency=750,time=.08){if(!enabled)return;try{ctx=ctx||new (window.AudioContext||window.webkitAudioContext)();if(ctx.state==='suspended')ctx.resume();const osc=ctx.createOscillator(),gain=ctx.createGain(),t=ctx.currentTime;osc.type='sine';osc.frequency.setValueAtTime(frequency,t);gain.gain.setValueAtTime(.0001,t);gain.gain.exponentialRampToValueAtTime(.055,t+.014);gain.gain.exponentialRampToValueAtTime(.0001,t+time);osc.connect(gain).connect(ctx.destination);osc.start(t);osc.stop(t+time+.02);}catch{}}
 let lastSpeech=0;
 function speak(message){if(!enabled||!message||!window.speechSynthesis)return;const now=Date.now();if(now-lastSpeech<1100)return;lastSpeech=now;try{window.speechSynthesis.cancel();const utterance=new SpeechSynthesisUtterance(String(message).slice(0,120));utterance.lang='en-IN';utterance.rate=1.04;utterance.volume=.78;window.speechSynthesis.speak(utterance);}catch{}}
 button?.addEventListener('click',()=>{enabled=!enabled;localStorage.setItem(key,enabled?'1':'0');draw();if(enabled)tone(860,.1);else window.speechSynthesis?.cancel?.();});
 document.addEventListener('click',e=>{if(!enabled)return;const control=e.target.closest('button,a,.rail-link');if(control && control!==button && !control.disabled)tone(550,.034);});
 window.myraaSound={success(message){tone(940,.14);speak(message);},error(){tone(220,.17);},click(){tone(580,.04);}};
 draw();
})();
