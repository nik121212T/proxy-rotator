# BitBrowser

Dans Navigateurs, le panneau BitBrowser connecte le gestionnaire à une installation locale de BitBrowser. Installez BitBrowser depuis https://www.bitbrowser.net/, ouvrez-le, connectez votre compte et activez son API locale. Les droits API et quotas de profils dépendent du compte BitBrowser.

Renseignez le port local (54345 par défaut) et la clé API, puis cliquez sur « Connecter / vérifier ». La vérification ne crée aucun profil. La clé est chiffrée dans la base du gestionnaire et n’est jamais renvoyée au navigateur. Un champ vide conserve la clé enregistrée.

Choisissez ensuite BitBrowser dans le formulaire d’ouverture. Le premier lancement crée automatiquement un profil pour la rotation ou un profil pour chaque proxy fixe sélectionné. Les lancements suivants réutilisent ces profils et leurs cookies. Fermer une session ferme sa fenêtre et sa passerelle, sans supprimer le profil BitBrowser. Le gestionnaire vérifie son marqueur de propriété avant de modifier ou fermer un profil. Un profil déjà ouvert doit être fermé avant sa réutilisation.

Chaque profil utilise une passerelle HTTP sur 127.0.0.1, avec un port propre à sa session. Les identifiants des proxies restent dans le gestionnaire. Un pool vide bloque les requêtes dans la passerelle ; un proxy fixe indisponible n’est pas remplacé. Gardez le gestionnaire et BitBrowser ouverts. En cas de perte de l’API, la passerelle s’arrête et une fermeture à réessayer est affichée.

Les profils persistants sont stockés et gérés par BitBrowser, séparément des moteurs Playwright. Leur suppression et leur stockage se gèrent dans BitBrowser. Si vous supprimez un profil ou changez de compte, son association locale devient inutilisable : le gestionnaire signale l’erreur sans modifier un autre profil. Une création interrompue peut avoir abouti côté BitBrowser ; vérifiez ses profils avant de réessayer.

Validation : client et cycle de session testés avec une API simulée. BitBrowser n’était pas installé sur le Mac de validation ; un essai réel avec le compte de l’utilisateur reste nécessaire, notamment pour vérifier les réglages réseau du moteur BitBrowser. Les arguments Chromium transmis ne constituent pas une certification d’absence de fuite réseau de ce navigateur tiers.

Documentation officielle : https://doc.bitbrowser.net/api-docs/browser-profiles et https://www.bitbrowser.net/local-api.
