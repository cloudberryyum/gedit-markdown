/* Offline Markdown rendering for the gedit Markdown Preview plugin. */
(function () {
    'use strict';

    var documentRoot = document.getElementById('document');
    var outlineRoot = document.getElementById('outline');
    var outlineLinks = document.getElementById('outline-links');
    var layout = document.getElementById('layout');
    var errorRoot = document.getElementById('error');
    var generation = 0;
    var diagramQueue = Promise.resolve();
    var currentOptions = { theme: 'light', outline: false, sync: true, baseUri: '', name: '' };
    var ignoreEchoUntil = 0;
    var scrollTimer = null;
    var scrollRevision = 0;
    var headingIds = new Map();

    function post(message) {
        var bridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.preview;
        if (!bridge || typeof bridge.postMessage !== 'function') {
            return false;
        }
        bridge.postMessage(JSON.stringify(message));
        return true;
    }

    function clamp(value) {
        return Math.max(0, Math.min(1, Number(value) || 0));
    }

    function scrollRatio() {
        var height = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
        return height ? clamp(window.scrollY / height) : 0;
    }

    function scrollToRatio(ratio) {
        ignoreEchoUntil = Date.now() + 200;
        window.scrollTo(0, clamp(ratio) * Math.max(0, document.documentElement.scrollHeight - window.innerHeight));
    }

    function capturePosition() {
        var position = { ratio: scrollRatio(), id: null, offset: 0, revision: scrollRevision };
        var headings = documentRoot.querySelectorAll('h1, h2, h3, h4, h5, h6');
        for (var i = 0; i < headings.length; i += 1) {
            var top = headings[i].getBoundingClientRect().top;
            if (top > 48) {
                break;
            }
            position.id = headings[i].id;
            position.offset = top;
        }
        return position;
    }

    function restorePosition(position, renderId) {
        if (renderId !== generation || position.revision !== scrollRevision) {
            return;
        }
        var heading = position.id && document.getElementById(position.id);
        ignoreEchoUntil = Date.now() + 200;
        if (heading) {
            window.scrollTo(0, window.scrollY + heading.getBoundingClientRect().top - position.offset);
        } else {
            scrollToRatio(position.ratio);
        }
    }

    function setOutline(enabled) {
        currentOptions.outline = Boolean(enabled);
        outlineRoot.hidden = !currentOptions.outline;
        layout.classList.toggle('has-outline', currentOptions.outline);
    }

    function makeElement(tag, className, text) {
        var element = document.createElement(tag);
        if (className) {
            element.className = className;
        }
        if (text !== undefined) {
            element.textContent = text;
        }
        return element;
    }

    function slug(text) {
        return text.normalize('NFKC').toLowerCase().replace(/[^\p{Letter}\p{Number}\s_-]/gu, '').trim().replace(/[\s_]+/g, '-') || 'section';
    }

    function buildOutline() {
        headingIds = new Map();
        outlineLinks.replaceChildren();
        var used = new Set();
        var headings = Array.from(documentRoot.querySelectorAll('h1, h2, h3, h4, h5, h6')).filter(function (heading) { return !heading.closest('.empty-state'); });
        headings.forEach(function (heading) {
            var stem = slug(heading.textContent);
            var unique = stem;
            var suffix = 1;
            while (used.has(unique)) {
                unique = stem + '-' + suffix;
                suffix += 1;
            }
            used.add(unique);
            heading.id = 'md-' + unique;
            headingIds.set(unique, heading.id);
            headingIds.set(heading.id, heading.id);
            var link = makeElement('a', 'outline-link outline-level-' + heading.tagName.substring(1), heading.textContent || 'Untitled section');
            link.setAttribute('href', '#' + heading.id);
            outlineLinks.appendChild(link);
        });
        if (!headings.length) {
            outlineLinks.appendChild(makeElement('p', 'outline-empty', 'Add headings to navigate this document.'));
        }
        return headings.length;
    }

    function allowedLocalImage(url) {
        if (!url.trim()) {
            return null;
        }
        if (/^data:image\/(?:png|jpe?g|gif|webp|avif|bmp|x-icon);base64,[a-z\d+/=\s]+$/i.test(url)) {
            return url;
        }
        try {
            var parsed = new URL(url, currentOptions.baseUri || window.location.href);
            if (parsed.protocol === 'file:' && (!parsed.hostname || parsed.hostname === 'localhost')) {
                return parsed.href;
            }
        } catch (error) {
            return null;
        }
        return null;
    }

    function prepareResources() {
        documentRoot.querySelectorAll('a').forEach(function (link) {
            var href = link.getAttribute('href') || '';
            link.removeAttribute('target');
            link.removeAttribute('download');
            if (href.charAt(0) === '#') {
                var fragment;
                try {
                    fragment = decodeURIComponent(href.substring(1));
                } catch (error) {
                    fragment = href.substring(1);
                }
                link.setAttribute('href', '#' + (headingIds.get(fragment) || fragment));
                return;
            }
            try {
                var url = new URL(href, currentOptions.baseUri || window.location.href);
                if (href && ['http:', 'https:', 'mailto:', 'file:'].indexOf(url.protocol) !== -1) {
                    link.setAttribute('href', url.href);
                    link.setAttribute('rel', 'noreferrer noopener');
                } else {
                    link.removeAttribute('href');
                }
            } catch (error) {
                link.removeAttribute('href');
            }
        });
        documentRoot.querySelectorAll('img').forEach(function (image) {
            var source = allowedLocalImage(image.getAttribute('src') || '');
            image.removeAttribute('srcset');
            if (source) {
                image.setAttribute('src', source);
                image.setAttribute('loading', 'lazy');
                image.setAttribute('referrerpolicy', 'no-referrer');
            } else {
                var label = image.getAttribute('alt') || 'Image';
                var placeholder = makeElement('span', 'image-placeholder', label + ' — remote image not loaded');
                placeholder.setAttribute('role', 'img');
                placeholder.setAttribute('aria-label', label + '. Only local images are loaded in preview.');
                image.replaceWith(placeholder);
            }
        });
        documentRoot.querySelectorAll('input').forEach(function (input) {
            if (input.type !== 'checkbox') {
                input.remove();
            } else {
                input.disabled = true;
                input.setAttribute('aria-label', input.checked ? 'Completed task' : 'Incomplete task');
            }
        });
        documentRoot.querySelectorAll('table').forEach(function (table) {
            var wrapper = makeElement('div', 'table-scroll');
            wrapper.tabIndex = 0;
            wrapper.setAttribute('role', 'region');
            wrapper.setAttribute('aria-label', 'Scrollable table');
            table.replaceWith(wrapper);
            wrapper.appendChild(table);
        });
    }

    function prepareCodeBlocks() {
        var diagrams = [];
        documentRoot.querySelectorAll('pre').forEach(function (pre) {
            var code = pre.querySelector('code');
            if (!code) {
                return;
            }
            var languageClass = Array.from(code.classList).find(function (name) { return name.indexOf('language-') === 0; });
            var language = languageClass ? languageClass.substring(9) : '';
            if (language.toLowerCase() === 'mermaid') {
                var placeholder = makeElement('figure', 'mermaid-diagram');
                placeholder.appendChild(makeElement('p', 'diagram-pending', 'Rendering diagram…'));
                pre.replaceWith(placeholder);
                diagrams.push({ source: code.textContent, element: placeholder });
                return;
            }
            var frame = makeElement('div', 'code-frame');
            var toolbar = makeElement('div', 'code-toolbar');
            toolbar.appendChild(makeElement('span', 'code-language', language || 'Plain text'));
            var copy = makeElement('button', 'copy-button', 'Copy');
            copy.type = 'button';
            copy.setAttribute('aria-label', 'Copy code');
            copy.addEventListener('click', function () {
                if (post({ kind: 'copy', text: code.textContent })) {
                    copy.textContent = 'Copied';
                    setTimeout(function () { copy.textContent = 'Copy'; }, 1400);
                } else {
                    copy.textContent = 'Clipboard unavailable';
                }
            });
            toolbar.appendChild(copy);
            pre.replaceWith(frame);
            frame.appendChild(toolbar);
            frame.appendChild(pre);
        });
        return diagrams;
    }

    function addDiagramSource(element, source, open) {
        var details = makeElement('details', 'diagram-source');
        details.open = open;
        details.appendChild(makeElement('summary', '', 'Diagram source'));
        var pre = makeElement('pre');
        pre.appendChild(makeElement('code', '', source));
        details.appendChild(pre);
        element.appendChild(details);
    }

    function svgImage(svg, purifier) {
        // SVG displayed as an image has its own CSS scope and no active DOM or callbacks.
        var safe = purifier.sanitize(svg, {
            USE_PROFILES: { svg: true, svgFilters: true },
            FORBID_TAGS: ['script', 'foreignObject', 'iframe', 'object', 'embed', 'a'],
            FORBID_ATTR: ['href', 'xlink:href', 'onload'],
            ALLOW_DATA_ATTR: false
        });
        var parsed = new DOMParser().parseFromString(safe, 'image/svg+xml');
        var root = parsed.documentElement;
        if (root.localName !== 'svg' || parsed.querySelector('parsererror')) {
            throw new Error('The diagram did not produce a valid SVG image.');
        }
        root.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
        var image = makeElement('img', 'diagram-image');
        image.alt = 'Mermaid diagram';
        var viewBox = (root.getAttribute('viewBox') || '').trim().split(/[\s,]+/).map(Number);
        if (viewBox.length === 4 && Number.isFinite(viewBox[2]) && viewBox[2] > 0) {
            image.width = Math.ceil(viewBox[2]);
        }
        image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(root));
        return image;
    }

    async function renderDiagrams(diagrams, renderId, theme, vendors) {
        if (renderId !== generation || !diagrams.length) {
            return 0;
        }
        vendors.mermaid.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            suppressErrorRendering: true,
            theme: theme === 'dark' ? 'dark' : 'default',
            htmlLabels: false,
            flowchart: { htmlLabels: false },
            fontFamily: 'system-ui, sans-serif',
            secure: ['secure', 'securityLevel', 'startOnLoad', 'suppressErrorRendering', 'maxTextSize', 'htmlLabels', 'themeCSS', 'themeVariables', 'fontFamily', 'flowchart'],
            maxTextSize: 100000
        });
        var errors = 0;
        for (var i = 0; i < diagrams.length; i += 1) {
            if (renderId !== generation) {
                break;
            }
            var diagram = diagrams[i];
            var staging = makeElement('div', 'diagram-render-staging');
            staging.setAttribute('aria-hidden', 'true');
            document.body.appendChild(staging);
            try {
                var result = await vendors.mermaid.render('diagram-' + renderId + '-' + i, diagram.source, staging);
                if (renderId !== generation) {
                    continue;
                }
                var canvas = makeElement('div', 'diagram-canvas');
                canvas.tabIndex = 0;
                canvas.setAttribute('role', 'region');
                canvas.setAttribute('aria-label', 'Scrollable Mermaid diagram');
                canvas.appendChild(svgImage(result.svg, vendors.DOMPurify));
                diagram.element.replaceChildren(canvas);
                addDiagramSource(diagram.element, diagram.source, false);
            } catch (error) {
                if (renderId !== generation) {
                    continue;
                }
                errors += 1;
                diagram.element.classList.add('diagram-error');
                var message = makeElement('p', 'diagram-error-message', 'Diagram could not be rendered. Check the Mermaid syntax below.');
                message.setAttribute('role', 'alert');
                diagram.element.replaceChildren(message);
                diagram.element.appendChild(makeElement('p', 'diagram-error-detail', String(error.message || error).slice(0, 450)));
                addDiagramSource(diagram.element, diagram.source, true);
            } finally {
                staging.remove();
            }
        }
        return errors;
    }

    function splitFrontmatter(source) {
        var match = source.match(/^\uFEFF?---[ \t]*\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/);
        return match ? { body: source.substring(match[0].length), properties: match[1] } : { body: source, properties: null };
    }

    async function render(source, options) {
        var renderId = ++generation;
        var position = capturePosition();
        var next = Object.assign({}, currentOptions, options || {});
        if (currentOptions.name && next.name !== currentOptions.name) {
            position = { ratio: 0, id: null, offset: 0, revision: scrollRevision };
        }
        currentOptions = next;
        document.documentElement.setAttribute('data-theme', next.theme === 'dark' ? 'dark' : 'light');
        document.title = next.name ? next.name + ' — Markdown Preview' : 'Markdown Preview';
        errorRoot.hidden = true;
        errorRoot.textContent = '';
        setOutline(next.outline);
        try {
            var vendors = window.PreviewVendors;
            if (!vendors || !vendors.markdownit || !vendors.DOMPurify || !vendors.hljs || !vendors.mermaid) {
                throw new Error('The offline preview libraries could not be loaded. Reinstall the plugin assets.');
            }
            var markdown = vendors.markdownit({
                html: true,
                linkify: true,
                typographer: false,
                highlight: function (code, language) {
                    if (language && language.toLowerCase() !== 'mermaid' && vendors.hljs.getLanguage(language)) {
                        try {
                            return vendors.hljs.highlight(code, { language: language, ignoreIllegals: true }).value;
                        } catch (error) {
                            return '';
                        }
                    }
                    return '';
                }
            });
            var validateLink = markdown.validateLink;
            markdown.validateLink = function (url) {
                return /^file:/i.test(url) || validateLink(url);
            };
            if (vendors.taskLists) {
                markdown.use(vendors.taskLists, { enabled: false, label: true, labelAfter: false });
            }
            var parts = splitFrontmatter(String(source || ''));
            var fragment = vendors.DOMPurify.sanitize(markdown.render(parts.body), {
                RETURN_DOM_FRAGMENT: true,
                USE_PROFILES: { html: true },
                ALLOW_DATA_ATTR: false,
                ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto|file):|data:image\/(?:png|jpe?g|gif|webp|avif|bmp|x-icon);base64,|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i,
                FORBID_TAGS: ['style', 'script', 'iframe', 'frame', 'frameset', 'object', 'embed', 'form', 'button', 'textarea', 'select', 'option', 'link', 'meta', 'base', 'video', 'audio', 'source', 'track', 'canvas', 'svg', 'math'],
                FORBID_ATTR: ['style', 'srcset', 'id', 'name', 'autofocus', 'contenteditable']
            });
            documentRoot.replaceChildren(fragment);
            if (parts.properties !== null) {
                var properties = makeElement('details', 'frontmatter');
                properties.appendChild(makeElement('summary', '', 'Properties'));
                var propertiesCode = makeElement('pre');
                propertiesCode.appendChild(makeElement('code', '', parts.properties));
                properties.appendChild(propertiesCode);
                documentRoot.prepend(properties);
            }
            if (!String(source || '').trim()) {
                var empty = makeElement('div', 'empty-state');
                empty.appendChild(makeElement('h2', '', 'Your preview is ready'));
                empty.appendChild(makeElement('p', '', 'Start writing Markdown in the editor. Headings, code blocks, and diagrams appear here.'));
                documentRoot.replaceChildren(empty);
            }
            var headings = buildOutline();
            prepareResources();
            var diagrams = prepareCodeBlocks();
            requestAnimationFrame(function () { restorePosition(position, renderId); });
            var task = diagramQueue.catch(function () {}).then(function () {
                return renderDiagrams(diagrams, renderId, next.theme, vendors);
            });
            diagramQueue = task;
            var errors = await task;
            if (renderId !== generation) {
                return;
            }
            requestAnimationFrame(function () { restorePosition(position, renderId); });
            post({ kind: 'ready', headings: headings, diagrams: diagrams.length, errors: errors });
        } catch (error) {
            if (renderId !== generation) {
                return;
            }
            documentRoot.replaceChildren();
            outlineLinks.replaceChildren();
            errorRoot.textContent = 'Preview could not be updated: ' + String(error.message || error);
            errorRoot.hidden = false;
            post({ kind: 'ready', headings: 0, diagrams: 0, errors: 1 });
        }
    }

    document.addEventListener('click', function (event) {
        var link = event.target.closest && event.target.closest('a[href^="#"]');
        if (!link) {
            return;
        }
        event.preventDefault();
        var target = document.getElementById(link.getAttribute('href').substring(1));
        if (target && documentRoot.contains(target)) {
            target.scrollIntoView({ block: 'start', behavior: 'auto' });
        }
    });

    window.addEventListener('scroll', function () {
        if (Date.now() < ignoreEchoUntil) {
            return;
        }
        scrollRevision += 1;
        if (!currentOptions.sync || scrollTimer !== null) {
            return;
        }
        scrollTimer = setTimeout(function () {
            scrollTimer = null;
            if (currentOptions.sync && Date.now() >= ignoreEchoUntil) {
                post({ kind: 'scroll', ratio: scrollRatio() });
            }
        }, 80);
    }, { passive: true });

    window.mdPreview = { render: render, scrollToRatio: scrollToRatio, setOutline: setOutline };
}());
