'''
@author:     Sid Probstein
@contact:    sid@swirl.today
'''

import time
import logging as logger
from datetime import datetime

from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.models import User, Group
from django.http import Http404, HttpResponse
from django.conf import settings

from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework import viewsets, status
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from swirl.models import *
from swirl.serializers import *
from swirl.models import SearchProvider, Search, Result
from swirl.serializers import UserSerializer, GroupSerializer, SearchProviderSerializer, SearchSerializer, ResultSerializer
from swirl.mixers import *

module_name = 'views.py'

from swirl.tasks import search_task, rescore_task
from swirl.search import search as execute_search

########################################

def index(request):
    context = {'index': []}
    return render(request, 'index.html', context)

########################################

class SearchProviderViewSet(viewsets.ModelViewSet):
    """
    ##S#W#I#R#L##1#.#7##############################################################
    API endpoint for managing SearchProviders. 
    Use GET to list all, POST to create a new one. 
    Add /<id>/ to DELETE, PUT or PATCH.
    """
    queryset = SearchProvider.objects.all()
    serializer_class = SearchProviderSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    pagination_class = None 

    def list(self, request):

        # check permissions
        if not request.user.has_perm('swirl.view_searchprovider'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        self.queryset = SearchProvider.objects.filter(owner=self.request.user)
        serializer = SearchProviderSerializer(self.queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def create(self, request):

        # check permissions
        if not request.user.has_perm('swirl.add_searchprovider'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        serializer = SearchProviderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(owner=self.request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

########################################

class SearchViewSet(viewsets.ModelViewSet):
    """
    ##S#W#I#R#L##1#.#7##############################################################
    API endpoint for managing Search objects. 
    Use GET to list all, POST to create a new one. 
    Add /<id>/ to DELETE, PUT or PATCH.
    Add ?q=<query_string> to the URL to create a Search with default settings
    Add ?rerun=<query_id> to fully re-execute a query, discarding previous results
    Add ?rescore=<query_id> to re-run post-result processing, updating relevancy scores
    """
    queryset = Search.objects.all()
    serializer_class = SearchSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]

    def list(self, request):

        # check permissions
        if not request.user.has_perm('swirl.view_search'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        providers = ""
        if 'providers' in request.GET.keys():
            providers = request.GET['providers']
            if ',' in providers:
                providers = providers.split(',')

        query_string = ""
        if 'q' in request.GET.keys():
            query_string = request.GET['q']
        if query_string:
            if providers:
                if type(providers) == list:
                    new_search = Search.objects.create(query_string=query_string,searchprovider_list=providers,owner=self.request.user)
                else:
                    new_search = Search.objects.create(query_string=query_string,searchprovider_list=[providers],owner=self.request.user)
                # end if
            else:
                new_search = Search.objects.create(query_string=query_string,owner=self.request.user)
            # end if
            new_search.status = 'NEW_SEARCH'
            new_search.save()
            search_task.delay(new_search.id)
            time.sleep(settings.SWIRL_Q_WAIT)
            return redirect(f'/swirl/results?search_id={new_search.id}')

        ########################################

        page = 1
        if 'page' in request.GET.keys():
            page = int(request.GET['page'])

        otf_result_mixer = None
        if 'result_mixer' in request.GET.keys():
            otf_result_mixer = str(request.GET['result_mixer'])

        explain = settings.SWIRL_EXPLAIN
        if 'explain' in request.GET.keys():
            explain = str(request.GET['explain'])
            if explain.lower() == 'false':
                explain = False
            elif explain.lower() == 'true':
                explain = True

        provider = None
        if 'provider' in request.GET.keys():
            provider = int(request.GET['provider'])

        query_string = ""
        if 'qx' in request.GET.keys():
            query_string = request.GET['qx']
        if query_string:
            if providers:
                if type(providers) == list:
                    new_search = Search.objects.create(query_string=query_string,searchprovider_list=providers,owner=self.request.user)
                else:
                    new_search = Search.objects.create(query_string=query_string,searchprovider_list=[providers],owner=self.request.user)
            else:
                new_search = Search.objects.create(query_string=query_string,owner=self.request.user)
            new_search.status = 'NEW_SEARCH'
            new_search.save()
            res = execute_search(new_search.id)
            if not res:
                return Response(f'Search failed: {new_search.status}!!', status=status.HTTP_500_INTERNAL_SERVER_ERROR)
            if Search.objects.filter(id=new_search.id).exists():
                search = Search.objects.get(id=new_search.id)
                if search.status.endswith('_READY') or search.status == 'RESCORING':
                    try:
                        if otf_result_mixer:
                            # call the specifixed mixer on the fly otf
                            results = eval(otf_result_mixer)(search.id, search.results_requested, page, explain, provider).mix()
                        else:
                            # call the mixer for this search provider
                            results = eval(search.result_mixer)(search.id, search.results_requested, page, explain, provider).mix()
                    except NameError as err:
                        message = f'Error: NameError: {err}'
                        logger.error(f'{module_name}: {message}')
                        return
                    except TypeError as err:
                        message = f'Error: TypeError: {err}'
                        logger.error(f'{module_name}: {message}')
                        return
                    return Response(results, status=status.HTTP_200_OK)
                else:
                    tries = tries + 1
                    time.sleep(1)
            else:
                # invalid search_id
                return Response('Result Object Not Found', status=status.HTTP_404_NOT_FOUND)
            # end if
        # end if

        ########################################

        rerun_id = 0
        if 'rerun' in request.GET.keys():
            rerun_id = int(request.GET['rerun'])

        if rerun_id:
            rerun_search = Search.objects.get(id=rerun_id)
            old_results = Result.objects.filter(search_id=rerun_search.id)
            # to do: instead of deleting, copy the search copy to a new search? 
            logger.warning(f"{module_name}: deleting Result objects associated with search {rerun_id}")
            for old_result in old_results:
                old_result.delete()
            rerun_search.status = 'NEW_SEARCH'
            # fix for https://github.com/sidprobstein/swirl-search/issues/35
            message = f"Re-run on {datetime.now()}"
            rerun_search.messages = []
            rerun_search.messages.append(message)    
            rerun_search.save()
            search_task.delay(rerun_search.id)
            time.sleep(settings.SWIRL_RERUN_WAIT)
            return redirect(f'/swirl/results?search_id={rerun_search.id}')
        # end if        

        ########################################

        rescore_id = 0
        if 'rescore' in request.GET.keys():
            rescore_id = request.GET['rescore']

        if rescore_id:
            # to do to do
            rescore_task.delay(rescore_id)
            time.sleep(settings.SWIRL_RESCORE_WAIT)
            return redirect(f'/swirl/results?search_id={rescore_id}')
        
        ########################################

        self.queryset = Search.objects.filter(owner=self.request.user)
        serializer = SearchSerializer(self.queryset, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    ########################################

    def create(self, request):

        # check permissions
        if not request.user.has_perm('swirl.add_search'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        serializer = SearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(owner=self.request.user)
        search_task.delay(serializer.data['id'])

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    ########################################

    def retrieve(self, request, pk=None):

        # check permissions
        if not request.user.has_perm('swirl.view_search'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        if Search.objects.filter(pk=pk, owner=self.request.user).exists():
            self.queryset = Search.objects.get(pk=pk)
            serializer = SearchSerializer(self.queryset)
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response('Search Object Not Found', status=status.HTTP_404_NOT_FOUND)
        # end if

    ########################################

    def update(self, request, pk=None):

        # check permissions
        if not request.user.has_perm('swirl.change_search'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        if Search.objects.filter(pk=pk, owner=self.request.user).exists():
            search = Search.objects.get(pk=pk)
            search.date_updated = datetime.now()
            serializer = SearchSerializer(instance=search, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save(owner=self.request.user)
            # re-start queries if status appropriate
            if search.status == 'NEW_SEARCH':
                search_task.delay(search.id)
                # publish('search_create', serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        else:
            return Response('Search Object Not Found', status=status.HTTP_404_NOT_FOUND)

    ########################################
        
    def destroy(self, request, pk=None):

        # check permissions
        if not request.user.has_perm('swirl.delete_search'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        if Search.objects.filter(pk=pk, owner=self.request.user).exists():
            search = Search.objects.get(pk=pk)
            search.delete()
            return Response('Search Object Deleted', status=status.HTTP_410_GONE)
        else:
            return Response('Search Object Not Found', status=status.HTTP_404_NOT_FOUND)

########################################

class ResultViewSet(viewsets.ModelViewSet):
    """
    ##S#W#I#R#L##1#.#7##############################################################
    API endpoint for managing Result objects, including Mixed Results
    Use GET to list all, POST to create a new one. 
    Add /<id>/ to DELETE, PUT or PATCH.
    Add ?search_id=<search_id> to the base URL to view mixed results with the default mixer
    Add &result_mixer=<MixerName> to the above URL specify the result mixer to use
    Add &explain=True to display the relevancy explanation for each result
    Add &provider=<provider_id> to filter results to one SearchProvider
    """
    queryset = Result.objects.all()
    serializer_class = ResultSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]

    def list(self, request):

        # check permissions
        if not request.user.has_perm('swirl.view_result'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        search_id = 0
        if 'search_id' in request.GET.keys():
            search_id = int(request.GET['search_id'])

        page = 1
        if 'page' in request.GET.keys():
            page = int(request.GET['page'])

        otf_result_mixer = None
        if 'result_mixer' in request.GET.keys():
            otf_result_mixer = str(request.GET['result_mixer'])

        explain = settings.SWIRL_EXPLAIN
        if 'explain' in request.GET.keys():
            explain = str(request.GET['explain'])
            if explain.lower() == 'false':
                explain = False
            elif explain.lower() == 'true':
                explain = True

        provider = None
        if 'provider' in request.GET.keys():
            provider = int(request.GET['provider'])

        if search_id:
            # check if the query has ready status
            if Search.objects.filter(id=search_id).exists():
                search = Search.objects.get(id=search_id)
                if search.status.endswith('_READY') or search.status == 'RESCORING':
                    try:
                        if otf_result_mixer:
                            # call the specifixed mixer on the fly otf
                            results = eval(otf_result_mixer)(search.id, search.results_requested, page, explain, provider).mix()
                        else:
                            # call the mixer for this search provider
                            results = eval(search.result_mixer)(search.id, search.results_requested, page, explain, provider).mix()
                    except NameError as err:
                        message = f'Error: NameError: {err}'
                        logger.error(f'{module_name}: {message}')
                        return
                    except TypeError as err:
                        message = f'Error: TypeError: {err}'
                        logger.error(f'{module_name}: {message}')
                        return
                    return Response(results, status=status.HTTP_200_OK)
                else:
                    return Response('Result Object Not Ready Yet', status=status.HTTP_503_SERVICE_UNAVAILABLE)
                # end if
            else:
                # invalid search_id
                return Response('Result Object Not Found', status=status.HTTP_404_NOT_FOUND)
        else:
            self.queryset = reversed(Result.objects.filter(owner=self.request.user))
            serializer = ResultSerializer(self.queryset, many=True)
            return Response(serializer.data, status=status.HTTP_200_OK)
        # end if

    ########################################

    def retrieve(self, request, pk=None):

        # check permissions
        if not request.user.has_perm('swirl.view_result'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        if Result.objects.filter(pk=pk, owner=self.request.user).exists():
            result = Result.objects.get(pk=pk)
            serializer = ResultSerializer(result)
            return Response(serializer.data, status=status.HTTP_200_OK)
        else:
            return Response('Result Object Not Found', status=status.HTTP_404_NOT_FOUND)
        # end if

    ########################################

    def update(self, request, pk=None):

        # check permissions
        if not request.user.has_perm('swirl.change_result'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        if Result.objects.filter(pk=pk, owner=self.request.user).exists():
            result = Result.objects.get(pk=pk)
            result.date_updated = datetime.now()
            serializer = ResultSerializer(instance=result, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save(owner=self.request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        else:
            return Response('Search Object Not Found', status=status.HTTP_404_NOT_FOUND)

    ########################################
        
    def destroy(self, request, pk=None):

        # check permissions
        if not request.user.has_perm('swirl.delete_result'):
            return Response(status=status.HTTP_403_FORBIDDEN)

        if Result.objects.filter(pk=pk, owner=self.request.user).exists():
            result = Result.objects.get(pk=pk)
            result.delete()
            return Response('Result Object Deleted!', status=status.HTTP_410_GONE)
        else:
            return Response('Result Object Not Found', status=status.HTTP_404_NOT_FOUND)

########################################

class UserViewSet(viewsets.ModelViewSet):
    """
    ##S#W#I#R#L##1#.#7##############################################################
    API endpoint that allows management of Users objects.
    Use GET to list all objects, POST to create a new one. 
    Add /<id>/ to DELETE, PUT or PATCH objects.
    """
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [permissions.IsAuthenticated]

########################################

class GroupViewSet(viewsets.ModelViewSet):
    """
    ##S#W#I#R#L##1#.#7##############################################################
    API endpoint that allows management of Group objects.
    Use GET to list all objects, POST to create a new one. 
    Add /<id>/ to DELETE, PUT or PATCH objects.
    """
    queryset = Group.objects.all()
    serializer_class = GroupSerializer
    authentication_classes = [SessionAuthentication, BasicAuthentication]
    permission_classes = [permissions.IsAuthenticated]
