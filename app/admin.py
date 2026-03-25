from django.contrib import admin
from app.models import File, CSVs
from app.models import Coder, Organization, OrganizationHash
from app.models import Comment, Activity, Discuss
from app.models import Teacher, Student 

admin.site.register(File)
admin.site.register(CSVs)
admin.site.register(Coder)
admin.site.register(Organization)
admin.site.register(OrganizationHash)
admin.site.register(Comment)
admin.site.register(Activity)
admin.site.register(Discuss)
admin.site.register(Teacher)
admin.site.register(Student)

from .models import ContactMessage, FeatureSuggestion

@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'date')
    list_filter = ('date',)
    search_fields = ('name', 'email', 'message')
    readonly_fields = ('name', 'email', 'message', 'date')

@admin.register(FeatureSuggestion)
class FeatureSuggestionAdmin(admin.ModelAdmin):
    list_display = ('suggestion', 'date')
    list_filter = ('date',)
    search_fields = ('suggestion',)
    ordering = ('-date',)