import { initEveryLayout } from './everylayout/init';
import './index.scss';

initEveryLayout();

const cl = document.body.classList;
cl.remove('no-js');
cl.add('with-js');

const mainNavToggle = document.querySelector<HTMLElement>('.main-nav-toggle');
const mainNav = document.querySelector<HTMLElement>('.main-nav');

if (mainNavToggle && mainNav) {
  mainNavToggle.hidden = false;
  mainNav.hidden = mainNavToggle.getAttribute('aria-expanded') !== 'true';
  mainNavToggle.addEventListener('click', () => {
    const expanded = mainNavToggle.getAttribute('aria-expanded') === 'true';
    mainNavToggle.setAttribute('aria-expanded', String(!expanded));
    mainNav.hidden = expanded;
  });
}
